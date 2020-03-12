# Lint as: python3
# Copyright 2020 The Flax Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Machine Translation example.

This script trains a Transformer on a WMT dataset.
"""

import functools
import os
import time
from absl import app
from absl import flags
from absl import logging
from flax import jax_utils
from flax import nn
from flax import optim
import bleu
import decode
import input_pipeline
import models
from flax.metrics import tensorboard
from flax.training import checkpoints
from flax.training import common_utils
import jax
from jax import random
import jax.nn
import jax.numpy as jnp
import numpy as np
import sacrebleu
import tensorflow.compat.v2 as tf

FLAGS = flags.FLAGS

flags.DEFINE_string(
    'model_dir', default=None,
    help='Directory to store model data')

flags.DEFINE_integer(
    'batch_size', default=256,
    help='Per host batch size for training.')

flags.DEFINE_integer(
    'eval_batch_size', default=256,
    help='Per host eval batch size for training.')

flags.DEFINE_integer(
    'eval_frequency', default=1087,
    help='Frequency of eval during training, e.g. every 1000 steps.')

flags.DEFINE_integer(
    'num_train_steps', default=500000,
    help='Number of train steps.')

flags.DEFINE_integer(
    'num_eval_steps', default=20,
    help='Number of evaluation steps.')

flags.DEFINE_float(
    'learning_rate', default=0.0625,
    help='Base learning rate.')

flags.DEFINE_float(
    'label_smoothing', default=0.1,
    help='Cross entropy loss label smoothing.')

flags.DEFINE_float(
    'weight_decay', default=0.0,
    help='Decay factor for AdamW style weight decay.')

flags.DEFINE_integer(
    'max_target_length', default=256,
    help='Maximum length of training examples.')

flags.DEFINE_integer(
    'max_eval_target_length', default=97,
    help='Maximum length of eval examples.')

flags.DEFINE_integer(
    'max_predict_length', default=147,
    help='Maximum length for predicted tokens.')

flags.DEFINE_bool(
    'share_embeddings', default=True,
    help='Inputs and targets share embedding.')

flags.DEFINE_bool(
    'logits_via_embedding', default=True,
    help='Final logit transform uses embedding matrix transpose.')

flags.DEFINE_integer(
    'random_seed', default=0,
    help='Integer for PRNG random seed.')

flags.DEFINE_bool(
    'save_checkpoints', default=False,
    help='Whether to save model checkpoints for debugging.')

flags.DEFINE_bool(
    'restore_checkpoints', default=False,
    help='Whether to restore from existing model checkpoints.')

flags.DEFINE_integer(
    'checkpoint_freq', default=10000,
    help='Whether to restore from existing model checkpoints.')

flags.DEFINE_bool(
    'use_bfloat16', default=True,
    help=('Use bfloat16 mixed precision training instead of float32.'))


def create_learning_rate_scheduler(
    factors='constant * linear_warmup * rsqrt_decay',
    base_learning_rate=0.5,
    warmup_steps=1000,
    decay_factor=0.5,
    steps_per_decay=20000,
    steps_per_cycle=100000):
  """creates learning rate schedule.

  Interprets factors in the factors string which can consist of:
  * constant: interpreted as the constant value,
  * linear_warmup: interpreted as linear warmup until warmup_steps,
  * rsqrt_decay: divide by square root of max(step, warmup_steps)
  * decay_every: Every k steps decay the learning rate by decay_factor.
  * cosine_decay: Cyclic cosine decay, uses steps_per_cycle parameter.

  Args:
    factors: a string with factors separated by '*' that defines the schedule.
    base_learning_rate: float, the starting constant for the lr schedule.
    warmup_steps: how many steps to warm up for in the warmup schedule.
    decay_factor: The amount to decay the learning rate by.
    steps_per_decay: How often to decay the learning rate.
    steps_per_cycle: Steps per cycle when using cosine decay.

  Returns:
    a function learning_rate(step): float -> {'learning_rate': float}, the
    step-dependent lr.
  """
  factors = [n.strip() for n in factors.split('*')]

  def step_fn(step):
    """Step to learning rate function."""
    ret = 1.0
    for name in factors:
      if name == 'constant':
        ret *= base_learning_rate
      elif name == 'linear_warmup':
        ret *= jnp.minimum(1.0, step / warmup_steps)
      elif name == 'rsqrt_decay':
        ret /= jnp.sqrt(jnp.maximum(step, warmup_steps))
      elif name == 'rsqrt_normalized_decay':
        ret *= jnp.sqrt(warmup_steps)
        ret /= jnp.sqrt(jnp.maximum(step, warmup_steps))
      elif name == 'decay_every':
        ret *= (decay_factor**(step // steps_per_decay))
      elif name == 'cosine_decay':
        progress = jnp.maximum(0.0,
                               (step - warmup_steps) / float(steps_per_cycle))
        ret *= jnp.maximum(0.0,
                           0.5 * (1.0 + jnp.cos(jnp.pi * (progress % 1.0))))
      else:
        raise ValueError('Unknown factor %s.' % name)
    return jnp.asarray(ret, dtype=jnp.float32)

  return step_fn


@functools.partial(jax.jit, static_argnums=(1, 2, 3))
def create_model(key, input_shape, target_shape, model_kwargs):
  """Instantiate transformer model and associated autoregressive cache def."""
  model_def = models.Transformer.partial(**model_kwargs)
  with nn.attention.Cache().mutate() as cache_def:
    _, model = model_def.create_by_shape(key,
                                         [(input_shape, jnp.float32),
                                          (target_shape, jnp.float32)],
                                         cache=cache_def)
  return model, cache_def


def create_optimizer(model, learning_rate):
  optimizer_def = optim.Adam(
      learning_rate,
      beta1=0.9,
      beta2=0.98,
      eps=1e-9,
      weight_decay=FLAGS.weight_decay)
  optimizer = optimizer_def.create(model)
  optimizer = optimizer.replicate()
  return optimizer


def compute_weighted_cross_entropy(logits,
                                   targets,
                                   weights=None,
                                   label_smoothing=0.0):
  """Compute weighted cross entropy and entropy for log probs and targets.

  Args:
   logits: [batch, length, num_classes] float array.
   targets: categorical targets [batch, length] int array.
   weights: None or array of shape [batch, length]
   label_smoothing: label smoothing constant, used to determine the on and
     off values.

  Returns:
    Tuple of scalar loss and batch normalizing factor.
  """
  if logits.ndim != targets.ndim + 1:
    raise ValueError('Incorrect shapes. Got shape %s logits and %s targets' %
                     (str(logits.shape), str(targets.shape)))
  vocab_size = logits.shape[-1]
  confidence = 1.0 - label_smoothing
  low_confidence = (1.0 - confidence) / (vocab_size - 1)
  normalizing_constant = -(
      confidence * jnp.log(confidence) + (vocab_size - 1) *
      low_confidence * jnp.log(low_confidence + 1e-20))
  soft_targets = common_utils.onehot(
      targets, vocab_size, on_value=confidence, off_value=low_confidence)

  loss = -jnp.sum(soft_targets * nn.log_softmax(logits), axis=-1)
  loss = loss - normalizing_constant

  normalizing_factor = jnp.prod(targets.shape)
  if weights is not None:
    loss = loss * weights
    normalizing_factor = weights.sum()

  return loss.sum(), normalizing_factor


def compute_weighted_accuracy(logits, targets, weights=None):
  """Compute weighted accuracy for log probs and targets.

  Args:
   logits: [batch, length, num_classes] float array.
   targets: categorical targets [batch, length] int array.
   weights: None or array of shape [batch, length]

  Returns:
    Tuple of scalar loss and batch normalizing factor.
  """
  if logits.ndim != targets.ndim + 1:
    raise ValueError('Incorrect shapes. Got shape %s logits and %s targets' %
                     (str(logits.shape), str(targets.shape)))
  loss = jnp.equal(jnp.argmax(logits, axis=-1), targets)
  normalizing_factor = jnp.prod(logits.shape[:-1])
  if weights is not None:
    loss = loss * weights
    normalizing_factor = weights.sum()

  return loss.sum(), normalizing_factor


def compute_metrics(logits, labels, weights):
  """Compute summary metrics."""
  loss, weight_sum = compute_weighted_cross_entropy(logits, labels, weights,
                                                    FLAGS.label_smoothing)
  acc, _ = compute_weighted_accuracy(logits, labels, weights)
  metrics = {
      'loss': loss,
      'accuracy': acc,
      'denominator': weight_sum,
  }
  metrics = common_utils.psum(metrics)
  return metrics


def train_step(optimizer, batch, learning_rate_fn, dropout_rng=None):
  """Perform a single training step."""
  train_keys = ['inputs', 'targets',
                'inputs_position', 'targets_position',
                'inputs_segmentation', 'targets_segmentation']
  (inputs, targets,
   inputs_positions, targets_positions,
   inputs_segmentation, targets_segmentation) = [
       batch.get(k, None) for k in train_keys]

  weights = jnp.where(targets > 0, 1, 0).astype(jnp.float32)

  # We handle PRNG splitting inside the top pmap to improve efficiency.
  dropout_rng, new_dropout_rng = random.split(dropout_rng)

  def loss_fn(model):
    """loss function used for training."""
    with nn.stochastic(dropout_rng):
      logits = model(
          inputs,
          targets,
          use_bfloat16=FLAGS.use_bfloat16,
          inputs_positions=inputs_positions,
          targets_positions=targets_positions,
          inputs_segmentation=inputs_segmentation,
          targets_segmentation=targets_segmentation,
          train=True,
          cache=None)

    loss, weight_sum = compute_weighted_cross_entropy(logits, targets, weights,
                                                      FLAGS.label_smoothing)
    mean_loss = loss / weight_sum
    return mean_loss, logits

  step = optimizer.state.step
  lr = learning_rate_fn(step)
  new_optimizer, _, logits = optimizer.optimize(loss_fn, learning_rate=lr)
  metrics = compute_metrics(logits, targets, weights)
  metrics['learning_rate'] = lr

  return new_optimizer, metrics, new_dropout_rng


def eval_step(model, batch):
  """Calculate evaluation metrics on a batch."""
  inputs, targets = batch['inputs'], batch['targets']
  weights = jnp.where(targets > 0, 1.0, 0.0)
  logits = model(inputs, targets, use_bfloat16=FLAGS.use_bfloat16, train=False,
                 cache=None)
  return compute_metrics(logits, targets, weights)


def predict_step(inputs, model, cache):
  """Predict translation with fast decoding beam search on a batch."""
  batch_size = inputs.shape[0]
  beam_size = 4

  # Prepare transformer fast-decoder call for beam search:
  # for beam search, we need to set up our decoder model
  # to handle a batch size equal to batch_size * beam_size,
  # where each batch item's data is expanded in-place rather
  # than tiled.
  # i.e. if we denote each batch element subtensor as el[n]:
  # [el0, el1, el2] --> beamsize=2 --> [el0,el0,el1,el1,el2,el2]
  src_padding_mask = decode.flat_batch_beam_expand(
      (inputs > 0)[..., None], beam_size)
  tgt_padding_mask = decode.flat_batch_beam_expand(
      jnp.ones((batch_size, 1, 1)), beam_size)
  encoded_inputs = decode.flat_batch_beam_expand(
      model.encode(inputs, train=False, cache=None), beam_size)

  def tokens_ids_to_logits(flat_ids, flat_cache):
    """Token slice to logits from decoder model."""
    # --> [batch * beam, 1, vocab]
    with flat_cache.mutate() as new_flat_cache:
      flat_logits = model.decode(encoded_inputs,
                                 src_padding_mask,
                                 flat_ids,
                                 cache=new_flat_cache,
                                 shift=False,
                                 train=False,
                                 tgt_padding_mask=tgt_padding_mask)
    # Remove singleton sequence-length dimension
    # [batch * beam, 1, vocab] --> [batch * beam, vocab]
    flat_logits = flat_logits.squeeze(axis=1)
    return flat_logits, new_flat_cache

  # using the above-defined single-step decoder function, run a
  # beam search over possible sequences given input encoding.
  beam_seqs, _ = decode.beam_search(
      inputs,
      cache,
      tokens_ids_to_logits,
      beam_size=beam_size,
      alpha=0.6,
      eos_token=1,
      max_decode_len=FLAGS.max_predict_length)

  # beam search returns [n_batch, n_beam, n_length + 1] with beam dimension
  # sorted in increasing order of log-probability
  # return the highest scoring beam sequence, drop first dummy 0 token.
  return beam_seqs[:, -1, 1:]


def pad_examples(x, desired_batch_size):
  """Expand batch to desired size by repeating last slice."""
  batch_pad = desired_batch_size - x.shape[0]
  return np.concatenate([x, np.tile(x[-1], (batch_pad, 1))], axis=0)


def tohost(x):
  """Collect batches from all devices to host and flatten batch dimensions."""
  n_device, n_batch, *remaining_dims = x.shape
  return np.array(x).reshape((n_device * n_batch,) + tuple(remaining_dims))


def main(argv):
  if len(argv) > 1:
    raise app.UsageError('Too many command-line arguments.')

  # This seems to be necessary even when importing TF2?
  tf.enable_v2_behavior()

  batch_size = FLAGS.batch_size
  learning_rate = FLAGS.learning_rate
  num_train_steps = FLAGS.num_train_steps
  num_eval_steps = FLAGS.num_eval_steps
  eval_freq = FLAGS.eval_frequency
  max_target_length = FLAGS.max_target_length
  max_eval_target_length = FLAGS.max_eval_target_length
  max_length = max(max_target_length, max_eval_target_length)
  random_seed = FLAGS.random_seed

  if jax.host_id() == 0:
    train_summary_writer = tensorboard.SummaryWriter(
        os.path.join(FLAGS.model_dir, 'train'))
    eval_summary_writer = tensorboard.SummaryWriter(
        os.path.join(FLAGS.model_dir, 'eval'))

  if (batch_size % jax.local_device_count() or
      FLAGS.eval_batch_size % jax.local_device_count()):
    raise ValueError('Batch size must be divisible by the number of devices')

  device_batch_size = batch_size // jax.local_device_count()
  train_ds, eval_ds, predict_ds, encoder = input_pipeline.get_wmt_datasets(
      n_devices=jax.local_device_count(),
      batch_size=batch_size,
      max_target_length=max_target_length,
      max_eval_target_length=max_eval_target_length)

  input_encoder = encoder
  target_encoder = encoder
  vocab_size = input_encoder.vocab_size
  output_vocab_size = target_encoder.vocab_size

  train_iter = iter(train_ds)
  input_shape = (batch_size, max_target_length)
  target_shape = (batch_size, max_target_length)

  transformer_kwargs = {
      'vocab_size': vocab_size,
      'output_vocab_size': output_vocab_size,
      'emb_dim': 1024,
      'num_heads': 16,
      'num_layers': 6,
      'qkv_dim': 1024,
      'mlp_dim': 4096,
      'max_len': max_length,
      'share_embeddings': FLAGS.share_embeddings,
      'logits_via_embedding': FLAGS.logits_via_embedding,
  }

  start_step = 0
  rng = random.PRNGKey(random_seed)
  rng, init_rng = random.split(rng)
  model, cache_def = create_model(init_rng,
                                  tuple(input_shape),
                                  tuple(target_shape),
                                  transformer_kwargs)
  optimizer = create_optimizer(model, learning_rate)
  del model  # don't keep a copy of the initial model
  if FLAGS.restore_checkpoints:
    # Restore unreplicated optimizer + model state from last checkpoint.
    optimizer = checkpoints.restore_checkpoint(FLAGS.model_dir, optimizer)
    # Grab last step from the first of the optimizer replicas.
    start_step = int(optimizer.state.step[0])

  learning_rate_fn = create_learning_rate_scheduler(
      base_learning_rate=learning_rate)

  p_train_step = jax.pmap(
      functools.partial(train_step, learning_rate_fn=learning_rate_fn),
      axis_name='batch')
  p_eval_step = jax.pmap(eval_step, axis_name='batch')
  p_pred_step = jax.pmap(predict_step, axis_name='batch')

  # We init the first set of dropout PRNG keys, but update it afterwards inside
  # the main pmap'd training update for performance.
  dropout_rngs = random.split(rng, jax.local_device_count())

  metrics_all = []
  t_loop_start = t_train_start = time.time()
  for step, batch in zip(range(start_step, num_train_steps), train_iter):
    # Shard data to devices and do a training step.
    batch = common_utils.shard(jax.tree_map(lambda x: x._numpy(), batch))  # pylint: disable=protected-access
    optimizer, metrics, dropout_rngs = p_train_step(
        optimizer, batch, dropout_rng=dropout_rngs)
    metrics_all.append(metrics)

    # Save a Checkpoint
    if step % FLAGS.checkpoint_freq == 0 and step > 0:
      if jax.host_id() == 0 and FLAGS.save_checkpoints:
        checkpoints.save_checkpoint(FLAGS.model_dir, optimizer, step)

    # Periodic metric handling.
    if step % eval_freq == 0:
      t_train_stop = time.time()

      # Training Metrics
      metrics_all = common_utils.get_metrics(metrics_all)
      lr = metrics_all.pop('learning_rate').mean()
      metrics_sums = jax.tree_map(jnp.sum, metrics_all)
      denominator = metrics_sums.pop('denominator')
      summary = jax.tree_map(lambda x: x / denominator, metrics_sums)  # pylint: disable=cell-var-from-loop
      summary['learning_rate'] = lr
      summary['perplexity'] = jnp.clip(jnp.exp(summary['loss']), a_max=1.0e4)
      logging.info('train in step: %d, loss: %.4f', step, summary['loss'])
      steps_per_eval = eval_freq if step != 0 else 1
      steps_per_sec = steps_per_eval / (time.time() - t_loop_start)
      train_steps_per_sec = steps_per_eval / (t_train_stop - t_train_start)
      t_loop_start = time.time()
      logging.info('train time: %.4f s step %d',
                   t_train_stop-t_train_start, step)
      if jax.host_id() == 0:
        train_summary_writer.scalar('steps per second', steps_per_sec, step)
        train_summary_writer.scalar('training steps per second',
                                    train_steps_per_sec, step)
        for key, val in summary.items():
          train_summary_writer.scalar(key, val, step)
        train_summary_writer.flush()
      metrics_all = []

      # Eval Metrics
      t_eval_start = time.time()
      eval_metrics = []
      eval_iter = iter(eval_ds)
      for _, eval_batch in zip(range(num_eval_steps), eval_iter):
        eval_batch = jax.tree_map(lambda x: x._numpy(), eval_batch)  # pylint: disable=protected-access
        eval_batch = common_utils.shard(eval_batch)
        metrics = p_eval_step(optimizer.target, eval_batch)
        eval_metrics.append(metrics)
      eval_metrics = common_utils.get_metrics(eval_metrics)
      eval_metrics_sums = jax.tree_map(jnp.sum, eval_metrics)
      eval_denominator = eval_metrics_sums.pop('denominator')
      eval_summary = jax.tree_map(
          lambda x: x / eval_denominator,  # pylint: disable=cell-var-from-loop
          eval_metrics_sums)
      eval_summary['perplexity'] = jnp.clip(
          jnp.exp(eval_summary['loss']), a_max=1.0e4)
      logging.info('eval in step: %d, loss: %.4f', step, eval_summary['loss'])
      if jax.host_id() == 0:
        for key, val in eval_summary.items():
          eval_summary_writer.scalar(key, val, step)
        eval_summary_writer.flush()
      logging.info('eval time: %.4f s step %d', time.time()-t_eval_start, step)

      # Translation and BLEU Score.
      t_inference_start = time.time()
      predict_iter = iter(predict_ds)
      sources, references, predictions = [], [], []
      for _, pred_batch in enumerate(predict_iter):
        pred_batch = jax.tree_map(lambda x: x._numpy(), pred_batch)  # pylint: disable=protected-access
        # Handle final odd-sized batch by padding instead of dropping it.
        cur_pred_batch_size = pred_batch['inputs'].shape[0]
        if cur_pred_batch_size != FLAGS.eval_batch_size:
          logging.info('Translation: uneven batch size %d.',
                       cur_pred_batch_size)
          pred_batch = jax.tree_map(
              lambda x: pad_examples(x, FLAGS.eval_batch_size), pred_batch)
        pred_batch = common_utils.shard(pred_batch)
        per_device_batchsize = pred_batch['inputs'].shape[1]
        cache = jax_utils.replicate(
            cache_def.initialize_cache((per_device_batchsize,
                                        FLAGS.max_predict_length)))
        predicted = p_pred_step(pred_batch['inputs'], optimizer.target, cache)
        predicted = tohost(predicted)
        inputs = tohost(pred_batch['inputs'])
        targets = tohost(pred_batch['targets'])
        # Iterate through non-padding examples of batch.
        for i, s in enumerate(predicted[:cur_pred_batch_size]):
          sources.append(input_encoder.decode(inputs[i]))
          references.append(target_encoder.decode(targets[i]))
          # TODO(levskaya): debug very rare initial 0-token predictions.
          try:
            predictions.append(target_encoder.decode(s))
          except ValueError:
            logging.error('bad predicted tokens: %s', s)
            predictions.append('Wir haben technische Schwierigkeiten.')
      logging.info('inference time: %.4f s step %d.',
                   time.time()-t_inference_start, step)
      logging.info('Translation: %d predictions %d references %d sources.',
                   len(predictions), len(references), len(sources))

      # Calculate BLEU score for translated eval corpus against reference.
      t_bleu_start = time.time()
      bleu_score = bleu.bleu_local(references, predictions)
      logging.info('bleu time: %.4f s step %d', time.time()-t_bleu_start, step)
      t_bleu_start = time.time()
      sacrebleu_score = sacrebleu.corpus_bleu(predictions, [references]).score
      logging.info('sacrebleu time: %.4f s step %d',
                   time.time()-t_bleu_start, step)
      # Save translation samples for tensorboard.
      exemplars = ''
      for n in np.random.choice(np.arange(len(predictions)), 8):
        exemplars += f'{sources[n]}\n\n{references[n]}\n\n{predictions[n]}\n\n'
      if jax.host_id() == 0:
        eval_summary_writer.scalar('bleu', bleu_score, step)
        eval_summary_writer.scalar('sacrebleu', sacrebleu_score, step)
        eval_summary_writer.text('samples', exemplars, step)
        eval_summary_writer.flush()

      # restart training-only timer
      t_train_start = time.time()

if __name__ == '__main__':
  app.run(main)