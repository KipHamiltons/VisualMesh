#!/usr/bin/env python3

import os
import sys
import random
import tensorflow as tf
from tensorflow.python.client import device_lib
import copy
import yaml
import re
import io
import cv2
import time
import numpy as np
import matplotlib as mpl
mpl.use('Agg')
import matplotlib.pyplot as plt

from . import network
from . import dataset


def save_yaml_model(sess, output_path, global_step):

  # Run tf to get all our variables
  variables = {v.name: sess.run(v) for v in tf.trainable_variables()}
  output = []

  # So we know when to move to the next list
  conv = -1
  layer = -1

  # Convert the keys into useful data
  items = []
  for k, v in variables.items():
    info = re.match(r'Network/Conv_(\d+)/Layer_(\d+)/(Weights|Biases):0', k)
    if info:
      items.append(((int(info.group(1)), int(info.group(2)), info.group(3).lower()), v))

  # Sorted so we see earlier layers first
  for k, v in sorted(items):
    c = k[0]
    l = k[1]
    var = k[2]

    # If we change convolution add a new element
    if c != conv:
      output.append([])
      conv = c
      layer = -1

    # If we change layer add a new object
    if l != layer:
      output[-1].append({})
      layer = l

    output[conv][layer][var] = v.tolist()

  # Print as yaml
  os.makedirs(os.path.join(output_path, 'yaml_models'), exist_ok=True)
  with open(os.path.join(output_path, 'yaml_models', 'model_{}.yaml'.format(global_step)), 'w') as f:
    f.write(yaml.dump(output, width=120))


class MeshDrawer:

  def __init__(self, classes):
    self.classes = classes

  def mesh_image(self, raws, pxs, ns, X):
    # Find the edges of the X values
    cs = np.cumsum(ns)
    cs = np.concatenate([[0], cs]).tolist()
    ranges = list(zip(cs, cs[1:]))

    images = []

    for batch, raw in enumerate(raws):
      img = cv2.imdecode(np.fromstring(raw, np.uint8), cv2.IMREAD_COLOR)
      img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
      px = pxs[batch, :ns[batch] - 1]  # Skip the null point (which doesn't exist in px)
      x = X[ranges[batch][0]:ranges[batch][1] - 1]  # Skip the null point

      # Setup the display so everything is all at the correct resolution
      dpi = 80
      height, width, nbands = img.shape
      figsize = width / float(dpi), height / float(dpi)
      fig = plt.figure(figsize=figsize)
      ax = fig.add_axes([0, 0, 1, 1])
      ax.axis('off')

      # Image underlay
      ax.imshow(img, interpolation='nearest')

      if px.shape[0] > 2:
        # Now for each class, produce a contour plot
        for i, data in enumerate(self.classes):
          r, g, b = data[1]
          r /= 255
          g /= 255
          b /= 255

          ax.tricontour(
            px[:, 1],
            px[:, 0],
            x[:, i],
            levels=[0.5, 0.75, 0.9],
            colors=[(r, g, b, 0.33), (r, g, b, 0.66), (r, g, b, 1.0)]
          )

      ax.set(xlim=[0, width], ylim=[height, 0], aspect=1)
      data = io.BytesIO()
      fig.savefig(data, format='jpg', dpi=dpi)
      data.seek(0)
      result = cv2.imdecode(np.fromstring(data.read(), np.uint8), cv2.IMREAD_COLOR)
      images.append(cv2.cvtColor(result, cv2.COLOR_BGR2RGB))

      fig.clf()
      plt.close(fig)

    return np.stack(images)

  def tutor_image(self, raws, pxs, ns, A):
    # Find the edges of the X values
    cs = np.cumsum(ns)
    cs = np.concatenate([[0], cs]).tolist()
    ranges = list(zip(cs, cs[1:]))

    images = []

    for batch, raw in enumerate(raws):
      img = cv2.imdecode(np.fromstring(raw, np.uint8), cv2.IMREAD_COLOR)
      img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
      px = pxs[batch, :ns[batch] - 1]  # Skip the null point (which doesn't exist in px)
      a = A[ranges[batch][0]:ranges[batch][1] - 1]  # Skip the null point

      # Setup the display so everything is all at the correct resolution
      dpi = 80
      height, width, nbands = img.shape
      figsize = width / float(dpi), height / float(dpi)
      fig = plt.figure(figsize=figsize)
      ax = fig.add_axes([0, 0, 1, 1])
      ax.axis('off')

      # Image underlay
      ax.imshow(img, interpolation='nearest')

      if px.shape[0] > 2:
        # Make our tutor plot
        ax.tricontour(
          px[:, 1],
          px[:, 0],
          a,
          levels=[0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
          cmap=plt.get_cmap('jet'),
        )

      ax.set(xlim=[0, width], ylim=[height, 0], aspect=1)
      data = io.BytesIO()
      fig.savefig(data, format='jpg', dpi=dpi)
      data.seek(0)
      result = cv2.imdecode(np.fromstring(data.read(), np.uint8), cv2.IMREAD_COLOR)
      images.append(cv2.cvtColor(result, cv2.COLOR_BGR2RGB))

      fig.clf()
      plt.close(fig)

    return np.stack(images)


def _device_graph(data, network_structure, tutor_structure, config, network_optimiser, tutor_optimiser):
  # Create the network and tutor graph ops for this device
  with tf.variable_scope('Network'):
    X = network.build_network(data['X'], data['G'], network_structure)
  with tf.variable_scope('Tutor'):
    T = tf.squeeze(network.build_network(data['X'], data['G'], tutor_structure), axis=-1)
    # Apply sigmoid to the tutor network
    T = tf.nn.sigmoid(T)

  # First eliminate points that were masked out with alpha
  with tf.name_scope('AlphaMask'):
    S = tf.where(tf.greater(data['W'], 0))
    X = tf.gather_nd(X, S)
    Y = tf.gather_nd(data['Y'], S)
    T = tf.gather_nd(T, S)

  # Calculate the loss for the batch on this device
  u_loss, x_loss, t_loss = _loss(X, T, Y, config)

  # Calculate summary information for validation passes
  metrics = _metrics(X, Y, config)

  # Calculate the gradients for this device
  x_grads = network_optimiser.compute_gradients(x_loss)
  t_grads = tutor_optimiser.compute_gradients(t_loss)

  # Store the ops that have been done on this device
  return {
    'data': data,
    'X': X,
    'T': T,
    'loss': {
      'u': u_loss,
      'x': x_loss,
      't': t_loss
    },
    'grads': {
      'x': x_grads,
      't': t_grads
    },
    'metrics': metrics
  }


def _loss(X, T, Y, config):
  """Calculate the loss for the Network and the Tutor given the provided labels and configuration"""
  with tf.name_scope("Loss"):

    # Unweighted loss, before the tutor network applies
    unweighted_mesh_loss = tf.nn.softmax_cross_entropy_with_logits_v2(logits=X, labels=Y, axis=1)

    # Labels for the tutor are the absolute error
    tutor_labels = tf.reduce_sum(tf.square(tf.subtract(Y, tf.nn.softmax(X, axis=1))), axis=1) / 2.0

    # Only use gradients from areas where the tutor has larger error, this avoids a large number of smaller
    # gradients overpowering the areas where the network has legitimate error.
    # This technique means that the tutor network will never converge, but we don't ever want it to
    tutor_idx = tf.where(tf.greater(tf.abs(tutor_labels - T), config.training.tutor.threshold))

    # If we have no values that are inaccurate, we will take all the values as normal
    tutor_loss_cut = tf.losses.mean_squared_error(
      predictions=tf.gather_nd(T, tutor_idx),
      labels=tf.stop_gradient(tf.gather_nd(tutor_labels, tutor_idx)),
    )
    tutor_loss_full = tf.losses.mean_squared_error(
      predictions=T,
      labels=tf.stop_gradient(tutor_labels),
    )
    tutor_loss = tf.cond(tf.equal(tf.size(tutor_idx), 0), lambda: tutor_loss_full, lambda: tutor_loss_cut)

    # Calculate the loss weights for each of the classes
    scatters = []
    for i in range(len(config.network.classes)):
      # Indexes of truth samples for this class
      idx = tf.where(Y[:, i])
      pts = tf.gather_nd(T, idx) + config.training.tutor.base_weight
      pts = tf.divide(pts, tf.reduce_sum(pts))
      pts = tf.scatter_nd(idx, pts, tf.shape(T, out_type=tf.int64))

      # Either our weights, or if there were none, zeros
      scatters.append(tf.cond(tf.equal(tf.size(idx), 0), lambda: tf.zeros_like(T), lambda: pts))

    # Even if we don't have all classes, the weights should sum to 1
    active_classes = tf.cast(tf.count_nonzero(tf.stack([tf.count_nonzero(s) for s in scatters])), tf.float32)
    W = tf.add_n(scatters)
    W = tf.divide(W, active_classes)

    # Weighted mesh loss, sum rather than mean as we have already normalised based on number of points
    weighted_mesh_loss = tf.reduce_sum(tf.multiply(unweighted_mesh_loss, tf.stop_gradient(W)))

  return tf.reduce_mean(unweighted_mesh_loss), weighted_mesh_loss, tutor_loss


def _metrics(X, Y, config):
  with tf.name_scope('Metrics'):
    metrics = {}

    # Calculate our unweighted loss, and the actual prediction from the network
    network_loss = tf.nn.softmax_cross_entropy_with_logits_v2(logits=X, labels=Y, axis=1)
    X = tf.nn.softmax(X, axis=1)

    for i, c in enumerate(config.network.classes):

      # Get our confusion matrix
      predictions = tf.cast(tf.equal(tf.argmax(X, axis=1), i), tf.int32)
      labels = tf.cast(tf.equal(tf.argmax(Y, axis=1), i), tf.int32)
      tp = tf.cast(tf.count_nonzero(predictions * labels), tf.float32)
      tn = tf.cast(tf.count_nonzero((predictions - 1) * (labels - 1)), tf.float32)
      fp = tf.cast(tf.count_nonzero(predictions * (labels - 1)), tf.float32)
      fn = tf.cast(tf.count_nonzero((predictions - 1) * labels), tf.float32)

      # Get the loss for this specific class
      class_loss = tf.reduce_mean(tf.gather_nd(network_loss, tf.where(Y[:, i])))

      # Add to our metrics object
      metrics[c[0]] = {'loss': class_loss, 'tp': tp, 'tn': tn, 'fp': fp, 'fn': fn}

    # Count how many losses were non 0 (0 loss means there were none of this class in the batch)
    class_losses = [tf.count_nonzero(m['loss']) for k, m in metrics.items()]
    active_classes = tf.add_n([tf.count_nonzero(l) for l in class_losses])
    metrics['Global'] = {
      'loss': tf.divide(tf.add_n(class_losses), active_classes),
      'tp': tf.add_n([m['tp'] for k, m in metrics.items()]),
      'tn': tf.add_n([m['tn'] for k, m in metrics.items()]),
      'fp': tf.add_n([m['fp'] for k, m in metrics.items()]),
      'fn': tf.add_n([m['fn'] for k, m in metrics.items()]),
    }

  return metrics


def _merge_ops(device_ops):

  # Always merge on the CPU
  with tf.device('/device:CPU:0'):
    # Merge the results of the operations together
    u_loss = tf.add_n([op['loss']['u'] for op in device_ops]) / len(device_ops)
    x_loss = tf.add_n([op['loss']['x'] for op in device_ops]) / len(device_ops)
    t_loss = tf.add_n([op['loss']['t'] for op in device_ops]) / len(device_ops)

    # Merge the gradients together
    x_grads = []
    for grads in zip(*[op['grads']['x'] for op in device_ops]):
      # None gradients don't matter
      if not any([v[0] is None for v in grads]):
        x_grads.append((tf.divide(tf.add_n([v[0] for v in grads]), len(device_ops)), grads[0][1]))
    t_grads = []
    for grads in zip(*[op['grads']['t'] for op in device_ops]):
      # None gradients don't matter
      if not any([v[0] is None for v in grads]):
        t_grads.append((tf.divide(tf.add_n([v[0] for v in grads]), len(device_ops)), grads[0][1]))

    # Merge the metrics together
    def _merge_metrics(metrics):
      if type(metrics[0]) == dict:
        return {k: _merge_metrics([m[k] for m in metrics]) for k in metrics[0]}
      else:
        return tf.add_n(metrics)

    metrics = _merge_metrics([op['metrics'] for op in device_ops])

    # Divide all the losses here by the number of GPUs to correct scaling
    metrics = {k: {**m, 'loss': tf.divide(m['loss'], len(device_ops))} for k, m in metrics.items()}

    return {
      'data': [op['data'] for op in device_ops],
      'X': [op['X'] for op in device_ops],
      'T': [op['T'] for op in device_ops],
      'loss': {
        'u': u_loss,
        'x': x_loss,
        't': t_loss
      },
      'grads': {
        'x': x_grads,
        't': t_grads
      },
      'metrics': metrics
    }


def _build_training_graph(gpus, config):
  # Some variables must exist on the CPU
  with tf.device('/device:CPU:0'):
    # Optimiser, and global_step variables on the CPU
    global_step = tf.Variable(0, dtype=tf.int32, trainable=False, name='global_step')
    network_optimiser = tf.train.AdamOptimizer(learning_rate=config.training.learning_rate)
    tutor_optimiser = tf.train.GradientDescentOptimizer(learning_rate=config.training.tutor.learning_rate)

    # This iterator is used so we can swap datasets as we go
    handle = tf.placeholder(tf.string, shape=[])
    iterator = tf.data.Iterator.from_string_handle(
      handle, {
        'X': tf.float32,
        'Y': tf.float32,
        'G': tf.int32,
        'W': tf.float32,
        'n': tf.int32,
        'px': tf.float32,
        'raw': tf.string,
      }, {
        'X': [None, 3],
        'Y': [None, len(config.network.classes)],
        'G': [None, 7],
        'W': [None],
        'n': [None],
        'px': [None, 2],
        'raw': [None],
      }
    )

  # Calculate the structure for the network and the tutor
  network_structure = copy.deepcopy(config.network.structure)
  tutor_structure = copy.deepcopy(config.training.tutor.structure
                                 ) if 'structure' in config.training.tutor else copy.deepcopy(network_structure)

  # Set the final output sizes for the network and tutor network
  network_structure[-1].append(len(config.network.classes))
  tutor_structure[-1].append(1)

  # For each GPU build a classification network, a tutor network and a gradients calculator
  device_ops = []
  for i, gpu in enumerate(gpus):
    with tf.device(gpu), tf.name_scope('Tower_{}'.format(i)):
      device_ops.append(
        _device_graph(
          iterator.get_next(), network_structure, tutor_structure, config, network_optimiser, tutor_optimiser
        )
      )

  # If we have multiple GPUs we need to do a merge operation, otherwise just take the element
  ops = _merge_ops(device_ops) if len(device_ops) > 1 else device_ops[0]

  # Apply the gradients as part of the optimisation
  with tf.device('/device:CPU:0'):
    optimise_mesh_op = network_optimiser.apply_gradients(ops['grads']['x'], global_step=global_step)
    optimise_tutor_op = tutor_optimiser.apply_gradients(ops['grads']['t'])

  # Create the loss summary op
  with tf.name_scope('Training'):
    loss_summary_op = tf.summary.merge([
      tf.summary.scalar('Raw Loss', ops['loss']['u']),
      tf.summary.scalar('Weighted Loss', ops['loss']['x']),
      tf.summary.scalar('Tutor Loss', ops['loss']['t']),
    ])

  # Now use the metrics to calculate interesting validation details
  validation_summary_op = []
  for k, m in ops['metrics'].items():
    with tf.name_scope(k.title()):
      validation_summary_op.extend([
        tf.summary.scalar('Loss', m['loss']),
        tf.summary.scalar('Precision', m['tp'] / (m['tp'] + m['fp'])),
        tf.summary.scalar('Recall', m['tp'] / (m['tp'] + m['fn']))
      ])
  validation_summary_op = tf.summary.merge(validation_summary_op)

  # Return the graph operations we will want to run
  return {
    'handle': handle,
    'global_step': global_step,
    'train': {
      'train': [optimise_mesh_op, optimise_tutor_op],
      'loss': {
        'u': ops['loss']['u'],
        't': ops['loss']['t'],
      },
      'summary': loss_summary_op
    },
    'validate': {
      'summary': validation_summary_op
    },
    'image': {
      'summary': []  #TODO
    },
  }


# Train the network
def train(config, output_path):

  # Find the GPUs we have available and if we don't have any, fallback to CPU
  gpus = [x.name for x in device_lib.list_local_devices() if x.device_type == 'GPU']
  gpus = ['/device:CPU:0'] if len(gpus) == 0 else gpus

  # Build the training graph operations we need
  ops = _build_training_graph(gpus, config)
  global_step = ops['global_step']

  # Setup for tensorboard
  summary_writer = tf.summary.FileWriter(output_path, graph=tf.get_default_graph())

  # Create our model saver to save all the trainable variables and the global_step
  save_vars = {v.name: v for v in tf.trainable_variables()}
  save_vars.update({global_step.name: global_step})
  saver = tf.train.Saver(save_vars)

  # Load our training and validation dataset
  training_dataset, training_ds_stats = dataset.VisualMeshDataset(
    input_files=config.dataset.training,
    classes=config.network.classes,
    geometry=config.geometry,
    batch_size=config.training.batch_size // len(gpus),
    prefetch=tf.data.experimental.AUTOTUNE,
    variants=config.training.variants,
  ).build(stats=True)
  training_dataset = training_dataset.repeat(config.training.epochs).make_initializable_iterator()

  # Merge in the dataset stats into the training summary
  ops['train']['summary'] = tf.summary.merge([ops['train']['summary'], training_ds_stats])

  # Load our training and validation dataset
  validation_dataset = dataset.VisualMeshDataset(
    input_files=config.dataset.validation,
    classes=config.network.classes,
    geometry=config.geometry,
    batch_size=config.training.validation.batch_size // len(gpus),
    prefetch=tf.data.experimental.AUTOTUNE,
    variants={},  # No variations for validation
  ).build().repeat().make_one_shot_iterator()

  # Build our image dataset for drawing images
  image_dataset = dataset.VisualMeshDataset(
    input_files=config.dataset.validation,
    classes=config.network.classes,
    geometry=config.geometry,
    batch_size=config.training.validation.progress_images // len(gpus),
    prefetch=1,
    variants={},  # No variations for images
  ).build()
  image_dataset = image_dataset.take(1).repeat().make_one_shot_iterator()

  # Tensorflow session configuration
  tf_config = tf.ConfigProto()
  tf_config.allow_soft_placement = False
  tf_config.graph_options.build_cost_model = 1
  tf_config.gpu_options.allow_growth = True

  with tf.Session(config=tf_config) as sess:

    # Initialise global variables
    sess.run(tf.global_variables_initializer())

    # Path to model file
    model_path = os.path.join(output_path, 'model.ckpt')

    # If we are loading existing training data do that
    if os.path.isfile(os.path.join(output_path, 'checkpoint')):
      checkpoint_file = tf.train.latest_checkpoint(output_path)
      print('Loading model {}'.format(checkpoint_file))
      saver.restore(sess, checkpoint_file)
    else:
      print('Creating new model {}'.format(model_path))

    # Initialise our dataset and get our string handles for use
    sess.run([training_dataset.initializer])
    training_handle, validation_handle, image_handle = sess.run([
      training_dataset.string_handle(),
      validation_dataset.string_handle(),
      image_dataset.string_handle()
    ])

    while True:
      try:
        # Run our training step
        start = time.perf_counter()
        output = sess.run(ops['train'], feed_dict={ops['handle']: training_handle})
        summary_writer.add_summary(output['summary'], tf.train.global_step(sess, global_step))
        end = time.perf_counter()

        # Print batch info
        print(
          'Batch: {} ({:3g}s) Mesh Loss: {:3g} Tutor Loss: {:3g}'.format(
            tf.train.global_step(sess, global_step),
            (end - start),
            output['loss']['u'],
            output['loss']['t'],
          )
        )

        # Every N steps do our validation/summary step
        if tf.train.global_step(sess, global_step) % config.training.validation.frequency == 0:
          output = sess.run(ops['validate'], feed_dict={ops['handle']: validation_handle})
          summary_writer.add_summary(output['summary'], tf.train.global_step(sess, global_step))

        # Every N steps save our model
        if tf.train.global_step(sess, global_step) % config.training.save_frequency == 0:
          saver.save(sess, model_path, tf.train.global_step(sess, global_step))
          save_yaml_model(sess, output_path, tf.train.global_step(sess, global_step))

        # Every N steps show our image summary
        # if tf.train.global_step(sess, global_step) % config.training.validation.image_frequency == 0:
        #   summary = sess.run(image_summary, feed_dict={net['handle']: image_handle})
        #   summary_writer.add_summary(summary, tf.train.global_step(sess, global_step))

      # We have finished the dataset
      except tf.errors.OutOfRangeError:

        # Do a validation step
        summary, = sess.run([validation_summary], feed_dict={net['handle']: validation_handle})
        summary_writer.add_summary(summary, tf.train.global_step(sess, global_step))

        # Output some images
        summary = sess.run(image_summary, feed_dict={net['handle']: image_handle})
        summary_writer.add_summary(summary, tf.train.global_step(sess, global_step))

        # Save the model
        saver.save(sess, model_path, tf.train.global_step(sess, global_step))
        save_yaml_model(sess, output_path, tf.train.global_step(sess, global_step))

        print('Training done')
        break
