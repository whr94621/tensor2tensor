# coding=utf-8
# Copyright 2018 The Tensor2Tensor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Basic models for testing simple tasks."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
import numpy as np
import six

from tensor2tensor.layers import common_attention
from tensor2tensor.layers import common_hparams
from tensor2tensor.layers import common_layers
from tensor2tensor.utils import registry
from tensor2tensor.utils import t2t_model

import tensorflow as tf
slim = tf.contrib.slim


@registry.register_model
class NextFrameBasic(t2t_model.T2TModel):
  """Basic next-frame model, may take actions and predict rewards too."""

  def make_even_size(self, x):
    """Pad x to be even-sized on axis 1 and 2, but only if necessary."""
    shape = [dim if dim is not None else -1 for dim in x.get_shape().as_list()]
    if shape[1] % 2 == 0 and shape[2] % 2 == 0:
      return x
    if shape[1] % 2 == 0:
      x, _ = common_layers.pad_to_same_length(
          x, x, final_length_divisible_by=2, axis=2)
      return x
    if shape[2] % 2 == 0:
      x, _ = common_layers.pad_to_same_length(
          x, x, final_length_divisible_by=2, axis=1)
      return x
    x, _ = common_layers.pad_to_same_length(
        x, x, final_length_divisible_by=2, axis=1)
    x, _ = common_layers.pad_to_same_length(
        x, x, final_length_divisible_by=2, axis=2)
    return x

  def body(self, features):
    hparams = self.hparams
    filters = hparams.hidden_size
    kernel1, kernel2 = (3, 3), (4, 4)

    # Embed the inputs.
    inputs_shape = common_layers.shape_list(features["inputs"])
    # Using non-zero bias initializer below for edge cases of uniform inputs.
    x = tf.layers.dense(
        features["inputs"], filters, name="inputs_embed",
        bias_initializer=tf.random_normal_initializer(stddev=0.01))
    x = common_attention.add_timing_signal_nd(x)

    # Down-stride.
    layer_inputs = [x]
    for i in range(hparams.num_compress_steps):
      with tf.variable_scope("downstride%d" % i):
        layer_inputs.append(x)
        x = self.make_even_size(x)
        if i < hparams.filter_double_steps:
          filters *= 2
        x = tf.layers.conv2d(x, filters, kernel2, activation=common_layers.belu,
                             strides=(2, 2), padding="SAME")
        x = common_layers.layer_norm(x)

    # Add embedded action if present.
    if "input_action" in features:
      action = tf.reshape(features["input_action"][:, -1, :],
                          [-1, 1, 1, hparams.hidden_size])
      action_mask = tf.layers.dense(action, filters, name="action_mask")
      zeros_mask = tf.zeros(common_layers.shape_list(x)[:-1] + [filters],
                            dtype=tf.float32)
      x *= action_mask + zeros_mask

    # Run a stack of convolutions.
    for i in range(hparams.num_hidden_layers):
      with tf.variable_scope("layer%d" % i):
        y = tf.layers.conv2d(x, filters, kernel1, activation=common_layers.belu,
                             strides=(1, 1), padding="SAME")
        y = tf.nn.dropout(y, 1.0 - hparams.dropout)
        if i == 0:
          x = y
        else:
          x = common_layers.layer_norm(x + y)

    # Up-convolve.
    layer_inputs = list(reversed(layer_inputs))
    for i in range(hparams.num_compress_steps):
      with tf.variable_scope("upstride%d" % i):
        if i >= hparams.num_compress_steps - hparams.filter_double_steps:
          filters //= 2
        x = tf.layers.conv2d_transpose(
            x, filters, kernel2, activation=common_layers.belu,
            strides=(2, 2), padding="SAME")
        y = layer_inputs[i]
        shape = common_layers.shape_list(y)
        x = x[:, :shape[1], :shape[2], :]
        x = common_layers.layer_norm(x + y)
        x = common_attention.add_timing_signal_nd(x)

    # Cut down to original size.
    x = x[:, :inputs_shape[1], :inputs_shape[2], :]

    # Reward prediction if needed.
    if "target_reward" not in features:
      return x
    reward_pred = tf.reduce_mean(x, axis=[1, 2], keepdims=True)
    return {"targets": x, "target_reward": reward_pred}

  def infer(self, features, *args, **kwargs):  # pylint: disable=arguments-differ
    """Produce predictions from the model by running it."""
    del args, kwargs
    # Inputs and features preparation needed to handle edge cases.
    if not features:
      features = {}
    inputs_old = None
    if "inputs" in features and len(features["inputs"].shape) < 4:
      inputs_old = features["inputs"]
      features["inputs"] = tf.expand_dims(features["inputs"], 2)

    def logits_to_samples(logits):
      """Get samples from logits."""
      # If the last dimension is 1 then we're using L1/L2 loss.
      if common_layers.shape_list(logits)[-1] == 1:
        return tf.to_int32(tf.squeeze(logits, axis=-1))
      # Argmax in TF doesn't handle more than 5 dimensions yet.
      logits_shape = common_layers.shape_list(logits)
      argmax = tf.argmax(tf.reshape(logits, [-1, logits_shape[-1]]), axis=-1)
      return tf.reshape(argmax, logits_shape[:-1])

    # Get predictions.
    try:
      num_channels = self.hparams.problem.num_channels
    except AttributeError:
      num_channels = 1
    features["targets"] = tf.zeros(
        [self.hparams.batch_size, 1, 1, 1, num_channels], dtype=tf.int32)
    features["target_reward"] = tf.zeros(
        [self.hparams.batch_size, 1, 1], dtype=tf.int32)
    logits, _ = self(features)  # pylint: disable=not-callable
    if isinstance(logits, dict):
      results = {}
      for k, v in six.iteritems(logits):
        results[k] = logits_to_samples(v)
        results["%s_logits" % k] = v
    else:
      results = logits_to_samples(logits)

    # Restore inputs to not confuse Estimator in edge cases.
    if inputs_old is not None:
      features["inputs"] = inputs_old

    # Return results.
    return results


@registry.register_model
class NextFrameStochastic(NextFrameBasic):
  """Stochastic next-frame model."""

  def construct_latent_tower(self, images):
    """Builds convolutional latent tower for stochastic model.

    At training time this tower generates a latent distribution (mean and std)
    conditioned on the entire video. This latent variable will be fed to the
    main tower as an extra variable to be used for future frames prediction.
    At inference time, the tower is disabled and only returns latents sampled
    from N(0,1).
    If the multi_latent flag is on, a different latent for every timestep would
    be generated.

    Args:
      images: tensor of ground truth image sequences
    Returns:
      latent_mean: predicted latent mean
      latent_std: predicted latent standard deviation
      latent_loss: loss of the latent twoer
      samples: random samples sampled from standard guassian
    """
    sequence_length = len(images)

    with slim.arg_scope([slim.conv2d], reuse=False):
      stacked_images = tf.concat(images, 3)

      latent_enc1 = slim.conv2d(
          stacked_images,
          32, [3, 3],
          stride=2,
          scope="latent_conv1")
      latent_enc1 = slim.batch_norm(latent_enc1, scope="latent_bn1")

      latent_enc2 = slim.conv2d(
          latent_enc1,
          64, [3, 3],
          stride=2,
          scope="latent_conv2")
      latent_enc2 = slim.batch_norm(latent_enc2, scope="latent_bn2")

      latent_enc3 = slim.conv2d(
          latent_enc2,
          64, [3, 3],
          stride=1,
          scope="latent_conv3")
      latent_enc3 = slim.batch_norm(latent_enc3, scope="latent_bn3")

      latent_mean = slim.conv2d(
          latent_enc3,
          self.hparams.latent_channels, [3, 3],
          stride=2,
          activation_fn=None,
          scope="latent_mean")

      latent_std = slim.conv2d(
          latent_enc3,
          self.hparams.latent_channels, [3, 3],
          stride=2,
          scope="latent_std")

      latent_std += self.hparams.latent_std_min

    if self.hparams.multi_latent:
      # timestep x batch_size x latent_size
      samples = tf.random_normal(
          [sequence_length-1] + latent_mean.shape, 0, 1,
          dtype=tf.float32)
    else:
      # batch_size x latent_size
      samples = tf.random_normal(tf.shape(latent_mean), 0, 1, dtype=tf.float32)

    if self.hparams.mode == tf.estimator.ModeKeys.TRAIN:
      return latent_mean, latent_std, samples
    else:
      # No latent tower at inference time, just standard gaussian.
      return None, None, samples

  def construct_model(self,
                      images,
                      actions,
                      states,
                      k=-1,
                      use_state=False,
                      num_masks=10,
                      cdna=True,
                      dna=False,
                      context_frames=2):
    """Build convolutional lstm video predictor using CDNA, or DNA.

    Args:
      images: tensor of ground truth image sequences
      actions: tensor of action sequences
      states: tensor of ground truth state sequences
      k: constant used for scheduled sampling. -1 to feed in own prediction.
      use_state: True to include state and action in prediction
      num_masks: the number of different pixel motion predictions (and
                 the number of masks for each of those predictions)
      cdna: True to use Convoluational Dynamic Neural Advection (CDNA)
      dna: True to use Dynamic Neural Advection (DNA)
      context_frames: number of ground truth frames to pass in before
                      feeding in own predictions
    Returns:
      gen_images: predicted future image frames
      gen_states: predicted future states

    Raises:
      ValueError: if more than one network option specified or more than 1 mask
      specified for DNA model.
    """
    # Each image is being used twice, in latent tower and main tower.
    # This is to make sure we are using the *same* image for both, ...
    # ... given how TF queues work.
    images = [tf.identity(image) for image in images]

    if cdna + dna != 1:
      raise ValueError("More than one, or no network option specified.")

    img_height, img_width, color_channels = self.hparams.problem.frame_shape
    batch_size = common_layers.shape_list(images[0])[0]
    lstm_func = self.basic_conv_lstm_cell

    # Generated robot states and images.
    gen_states, gen_images = [], []
    current_state = states[0]

    if k == -1:
      feedself = True
    else:
      # Scheduled sampling:
      # Calculate number of ground-truth frames to pass in.
      iter_num = tf.train.get_or_create_global_step()
      num_ground_truth = tf.to_int32(
          tf.round(
              tf.to_float(batch_size) *
              (k / (k + tf.exp(tf.to_float(iter_num) / tf.to_float(k))))))
      feedself = False

    # LSTM state sizes and states.
    lstm_size = np.int32(np.array([32, 32, 64, 64, 128, 64, 32]))
    lstm_state1, lstm_state2, lstm_state3, lstm_state4 = None, None, None, None
    lstm_state5, lstm_state6, lstm_state7 = None, None, None

    # Latent tower
    if self.hparams.stochastic_model:
      latent_tower_outputs = self.construct_latent_tower(images)
      latent_mean, latent_std, samples = latent_tower_outputs

    # Main tower
    timestep = 0
    layer_norm = tf.contrib.layers.layer_norm

    for image, action in zip(images[:-1], actions[:-1]):
      # Reuse variables after the first timestep.
      reuse = bool(gen_images)

      done_warm_start = len(gen_images) > context_frames - 1
      with slim.arg_scope(
          [
              lstm_func, slim.layers.conv2d, slim.layers.fully_connected,
              layer_norm, slim.layers.conv2d_transpose
          ],
          reuse=reuse):

        if feedself and done_warm_start:
          # Feed in generated image.
          prev_image = gen_images[-1]
        elif done_warm_start:
          # Scheduled sampling
          prev_image = self.scheduled_sample(
              image, gen_images[-1], self.hparams.batch_size, num_ground_truth)
        else:
          # Always feed in ground_truth
          prev_image = image

        # Predicted state is always fed back in
        state_action = tf.concat(axis=1, values=[action, current_state])

        enc0 = slim.layers.conv2d(
            prev_image,
            32, [5, 5],
            stride=2,
            scope="scale1_conv1",
            normalizer_fn=layer_norm,
            normalizer_params={"scope": "layer_norm1"})

        hidden1, lstm_state1 = lstm_func(
            enc0, lstm_state1, lstm_size[0], scope="state1")
        hidden1 = layer_norm(hidden1, scope="layer_norm2")
        hidden2, lstm_state2 = lstm_func(
            hidden1, lstm_state2, lstm_size[1], scope="state2")
        hidden2 = layer_norm(hidden2, scope="layer_norm3")
        enc1 = slim.layers.conv2d(
            hidden2, hidden2.get_shape()[3], [3, 3], stride=2, scope="conv2")

        hidden3, lstm_state3 = lstm_func(
            enc1, lstm_state3, lstm_size[2], scope="state3")
        hidden3 = layer_norm(hidden3, scope="layer_norm4")
        hidden4, lstm_state4 = lstm_func(
            hidden3, lstm_state4, lstm_size[3], scope="state4")
        hidden4 = layer_norm(hidden4, scope="layer_norm5")
        enc2 = slim.layers.conv2d(
            hidden4, hidden4.get_shape()[3], [3, 3], stride=2, scope="conv3")

        # Pass in state and action.
        smear = tf.reshape(
            state_action,
            [-1, 1, 1, int(state_action.get_shape()[1])])
        smear = tf.tile(
            smear, [1, int(enc2.get_shape()[1]),
                    int(enc2.get_shape()[2]), 1])
        if use_state:
          enc2 = tf.concat(axis=3, values=[enc2, smear])

        # Setup latent
        if self.hparams.stochastic_model:
          latent = samples
          if self.hparams.multi_latent:
            latent = samples[timestep]
          if self.hparams.mode == tf.estimator.ModeKeys.TRAIN:
            # TODO(mbz): put 1st stage of training back in if necessary
            latent = latent_mean + tf.exp(latent_std / 2.0) * latent
          with tf.control_dependencies([latent]):
            enc2 = tf.concat([enc2, latent], 3)

        enc3 = slim.layers.conv2d(
            enc2, hidden4.get_shape()[3], [1, 1], stride=1, scope="conv4")

        hidden5, lstm_state5 = lstm_func(
            enc3, lstm_state5, lstm_size[4], scope="state5")  # last 8x8
        hidden5 = layer_norm(hidden5, scope="layer_norm6")
        enc4 = slim.layers.conv2d_transpose(
            hidden5, hidden5.get_shape()[3], 3, stride=2, scope="convt1")

        hidden6, lstm_state6 = lstm_func(
            enc4, lstm_state6, lstm_size[5], scope="state6")  # 16x16
        hidden6 = layer_norm(hidden6, scope="layer_norm7")
        # Skip connection.
        hidden6 = tf.concat(axis=3, values=[hidden6, enc1])  # both 16x16

        enc5 = slim.layers.conv2d_transpose(
            hidden6, hidden6.get_shape()[3], 3, stride=2, scope="convt2")
        hidden7, lstm_state7 = lstm_func(
            enc5, lstm_state7, lstm_size[6], scope="state7")  # 32x32
        hidden7 = layer_norm(hidden7, scope="layer_norm8")

        # Skip connection.
        hidden7 = tf.concat(axis=3, values=[hidden7, enc0])  # both 32x32

        enc6 = slim.layers.conv2d_transpose(
            hidden7,
            hidden7.get_shape()[3],
            3,
            stride=2,
            scope="convt3",
            activation_fn=None,
            normalizer_fn=layer_norm,
            normalizer_params={"scope": "layer_norm9"})

        if dna:
          # Using largest hidden state for predicting untied conv kernels.
          enc7 = slim.layers.conv2d_transpose(
              enc6,
              self.hparams.dna_kernel_size**2,
              1,
              stride=1,
              scope="convt4",
              activation_fn=None)
        else:
          # Using largest hidden state for predicting a new image layer.
          enc7 = slim.layers.conv2d_transpose(
              enc6,
              color_channels,
              1,
              stride=1,
              scope="convt4",
              activation_fn=None)
          # This allows the network to also generate one image from scratch,
          # which is useful when regions of the image become unoccluded.
          transformed = [tf.nn.sigmoid(enc7)]

        if cdna:
          # cdna_input = tf.reshape(hidden5, [int(batch_size), -1])
          cdna_input = tf.contrib.layers.flatten(hidden5)
          transformed += self.cdna_transformation(
              prev_image, cdna_input, num_masks, int(color_channels))
        elif dna:
          # Only one mask is supported (more should be unnecessary).
          if num_masks != 1:
            raise ValueError("Only one mask is supported for DNA model.")
          transformed = [self.dna_transformation(prev_image, enc7)]

        masks = slim.layers.conv2d_transpose(
            enc6, num_masks + 1, 1,
            stride=1, scope="convt7", activation_fn=None)
        masks = tf.reshape(
            tf.nn.softmax(tf.reshape(masks, [-1, num_masks + 1])),
            [batch_size,
             int(img_height),
             int(img_width), num_masks + 1])
        mask_list = tf.split(
            axis=3, num_or_size_splits=num_masks + 1, value=masks)
        output = mask_list[0] * prev_image
        for layer, mask in zip(transformed, mask_list[1:]):
          output += layer * mask
        gen_images.append(output)

        current_state = slim.layers.fully_connected(
            state_action,
            int(current_state.get_shape()[1]),
            scope="state_pred",
            activation_fn=None)
        gen_states.append(current_state)
        timestep += 1

    return gen_images, gen_states, latent_mean, latent_std

  def cdna_transformation(self,
                          prev_image,
                          cdna_input,
                          num_masks,
                          color_channels):
    """Apply convolutional dynamic neural advection to previous image.

    Args:
      prev_image: previous image to be transformed.
      cdna_input: hidden lyaer to be used for computing CDNA kernels.
      num_masks: number of masks and hence the number of CDNA transformations.
      color_channels: the number of color channels in the images.
    Returns:
      List of images transformed by the predicted CDNA kernels.
    """
    batch_size = tf.shape(cdna_input)[0]
    height = int(prev_image.get_shape()[1])
    width = int(prev_image.get_shape()[2])

    # Predict kernels using linear function of last hidden layer.
    cdna_kerns = slim.layers.fully_connected(
        cdna_input,
        self.hparams.dna_kernel_size *
        self.hparams.dna_kernel_size * num_masks,
        scope="cdna_params",
        activation_fn=None)

    # Reshape and normalize.
    cdna_kerns = tf.reshape(
        cdna_kerns, [batch_size, self.hparams.dna_kernel_size,
                     self.hparams.dna_kernel_size, 1, num_masks])
    cdna_kerns = (tf.nn.relu(cdna_kerns - self.hparams.relu_shift)
                  + self.hparams.relu_shift)
    norm_factor = tf.reduce_sum(cdna_kerns, [1, 2, 3], keep_dims=True)
    cdna_kerns /= norm_factor

    # Treat the color channel dimension as the batch dimension since the same
    # transformation is applied to each color channel.
    # Treat the batch dimension as the channel dimension so that
    # depthwise_conv2d can apply a different transformation to each sample.
    cdna_kerns = tf.transpose(cdna_kerns, [1, 2, 0, 4, 3])
    cdna_kerns = tf.reshape(cdna_kerns,
                            [self.hparams.dna_kernel_size,
                             self.hparams.dna_kernel_size,
                             batch_size,
                             num_masks])
    # Swap the batch and channel dimensions.
    prev_image = tf.transpose(prev_image, [3, 1, 2, 0])

    # Transform image.
    transformed = tf.nn.depthwise_conv2d(prev_image, cdna_kerns, [1, 1, 1, 1],
                                         "SAME")

    # Transpose the dimensions to where they belong.
    transformed = tf.reshape(
        transformed, [color_channels, height, width, batch_size, num_masks])
    transformed = tf.transpose(transformed, [3, 1, 2, 0, 4])
    transformed = tf.unstack(transformed, axis=-1)
    return transformed

  def dna_transformation(self,
                         prev_image,
                         dna_input):
    """Apply dynamic neural advection to previous image.

    Args:
      prev_image: previous image to be transformed.
      dna_input: hidden lyaer to be used for computing DNA transformation.
    Returns:
      List of images transformed by the predicted CDNA kernels.
    """
    # Construct translated images.
    prev_image_pad = tf.pad(prev_image, [[0, 0], [2, 2], [2, 2], [0, 0]])
    image_height = int(prev_image.get_shape()[1])
    image_width = int(prev_image.get_shape()[2])

    inputs = []
    for xkern in range(self.hparams.dna_kernel_size):
      for ykern in range(self.hparams.dna_kernel_size):
        inputs.append(
            tf.expand_dims(
                tf.slice(prev_image_pad, [0, xkern, ykern, 0],
                         [-1, image_height, image_width, -1]), [3]))
    inputs = tf.concat(axis=3, values=inputs)

    # Normalize channels to 1.
    kernel = (tf.nn.relu(dna_input -self.hparams.relu_shift)
              + self.hparams.relu_shift)
    kernel = tf.expand_dims(kernel / tf.reduce_sum(kernel, [3], keep_dims=True),
                            [4])
    return tf.reduce_sum(kernel * inputs, [3], keep_dims=False)

  def scheduled_sample(self,
                       ground_truth_x,
                       generated_x,
                       batch_size,
                       num_ground_truth):
    """Sample batch with specified mix of groundtruth and generated data points.

    Args:
      ground_truth_x: tensor of ground-truth data points.
      generated_x: tensor of generated data points.
      batch_size: batch size
      num_ground_truth: number of ground-truth examples to include in batch.
    Returns:
      New batch with num_ground_truth sampled from ground_truth_x and the rest
      from generated_x.
    """
    idx = tf.random_shuffle(tf.range(int(batch_size)))
    ground_truth_idx = tf.gather(idx, tf.range(num_ground_truth))
    generated_idx = tf.gather(idx, tf.range(num_ground_truth, int(batch_size)))

    ground_truth_examps = tf.gather(ground_truth_x, ground_truth_idx)
    generated_examps = tf.gather(generated_x, generated_idx)
    return tf.dynamic_stitch([ground_truth_idx, generated_idx],
                             [ground_truth_examps, generated_examps])

  def init_state(self,
                 inputs,
                 state_shape,
                 state_initializer=tf.zeros_initializer(),
                 dtype=tf.float32):
    """Helper function to create an initial state given inputs.

    Args:
      inputs: input Tensor, at least 2D, the first dimension being batch_size
      state_shape: the shape of the state.
      state_initializer: Initializer(shape, dtype) for state Tensor.
      dtype: Optional dtype, needed when inputs is None.
    Returns:
       A tensors representing the initial state.
    """
    # recoded by @mbz
    initial_state = tf.zeros([tf.shape(inputs)[0]] + state_shape)
    return initial_state

  # TODO(mbz): use tf.distributions.kl_divergence instead.
  def kl_divergence(self, mu, log_sigma):
    """KL divergence of diagonal gaussian N(mu,exp(log_sigma)) and N(0,1).

    Args:
      mu: mu parameter of the distribution.
      log_sigma: log(sigma) parameter of the distribution.
    Returns:
      the KL loss.
    """

    return -.5 * tf.reduce_sum(
        1. + log_sigma - tf.square(mu) - tf.exp(log_sigma),
        axis=1)

  @slim.add_arg_scope
  def basic_conv_lstm_cell(self,
                           inputs,
                           state,
                           num_channels,
                           filter_size=5,
                           forget_bias=1.0,
                           scope=None,
                           reuse=None):
    """Basic LSTM recurrent network cell, with 2D convolution connctions.

    We add forget_bias (default: 1) to the biases of the forget gate in order to
    reduce the scale of forgetting in the beginning of the training.
    It does not allow cell clipping, a projection layer, and does not
    use peep-hole connections: it is the basic baseline.
    Args:
      inputs: input Tensor, 4D, batch x height x width x channels.
      state: state Tensor, 4D, batch x height x width x channels.
      num_channels: the number of output channels in the layer.
      filter_size: the shape of the each convolution filter.
      forget_bias: the initial value of the forget biases.
      scope: Optional scope for variable_scope.
      reuse: whether or not the layer and the variables should be reused.
    Returns:
       a tuple of tensors representing output and the new state.
    """
    spatial_size = [v.value for v in inputs.get_shape()[1:3]]

    if state is None:
      state = self.init_state(inputs, spatial_size + [2 * num_channels])
    with tf.variable_scope(scope,
                           "BasicConvLstmCell",
                           [inputs, state],
                           reuse=reuse):
      inputs.get_shape().assert_has_rank(4)
      state.get_shape().assert_has_rank(4)
      c, h = tf.split(axis=3, num_or_size_splits=2, value=state)
      inputs_h = tf.concat(axis=3, values=[inputs, h])
      # Parameters of gates are concatenated into one conv for efficiency.
      i_j_f_o = slim.layers.conv2d(inputs_h,
                                   4 * num_channels, [filter_size, filter_size],
                                   stride=1,
                                   activation_fn=None,
                                   scope="Gates")

      # i = input_gate, j = new_input, f = forget_gate, o = output_gate
      i, j, f, o = tf.split(axis=3, num_or_size_splits=4, value=i_j_f_o)

      new_c = c * tf.sigmoid(f + forget_bias) + tf.sigmoid(i) * tf.tanh(j)
      new_h = tf.tanh(new_c) * tf.sigmoid(o)

      return new_h, tf.concat(axis=3, values=[new_c, new_h])

  def body(self, features):
    hparams = self.hparams

    # Split inputs and targets time-wise into a list of frames.
    input_frames = tf.unstack(features["inputs"], axis=1)
    target_frames = tf.unstack(features["targets"], axis=1)

    num_frames = hparams.problem.num_input_and_target_frames
    batch_size = common_layers.shape_list(input_frames)[0]
    fake_zeros = [tf.zeros((batch_size, 1), dtype=tf.float32)
                  for _ in range(num_frames)]
    is_training = self.hparams.mode == tf.estimator.ModeKeys.TRAIN
    gen_images, _, latent_mean, latent_std = self.construct_model(
        images=input_frames + target_frames,
        actions=fake_zeros,
        states=fake_zeros,
        k=900.0 if is_training else -1.0,
        use_state=False,
        num_masks=10,
        cdna=True,
        dna=False,
        context_frames=hparams.problem.num_input_frames)

    kl_loss = 0.0
    step_num = tf.train.get_or_create_global_step()
    beta = tf.cond(step_num > self.hparams.num_iterations_2nd_stage,
                   lambda: self.hparams.latent_loss_multiplier,
                   lambda: 0.0)

    if is_training:
      tf.summary.scalar("beta", beta)
      tf.summary.histogram("posterior_mean", latent_mean)
      tf.summary.histogram("posterior_std", latent_std)

    if is_training:
      kl_loss = self.kl_divergence(latent_mean, latent_std)
      tf.summary.scalar("kl_raw", tf.reduce_mean(kl_loss))
    kl_loss *= beta

    predictions = gen_images[hparams.problem.num_input_frames-1:]
    return predictions, kl_loss


@registry.register_hparams
def next_frame():
  """Basic 2-frame conv model."""
  hparams = common_hparams.basic_params1()
  hparams.hidden_size = 64
  hparams.batch_size = 4
  hparams.num_hidden_layers = 2
  hparams.optimizer = "Adafactor"
  hparams.learning_rate_constant = 1.5
  hparams.learning_rate_warmup_steps = 1500
  hparams.learning_rate_schedule = "linear_warmup * constant * rsqrt_decay"
  hparams.label_smoothing = 0.0
  hparams.initializer = "uniform_unit_scaling"
  hparams.initializer_gain = 1.3
  hparams.weight_decay = 0.0
  hparams.clip_grad_norm = 1.0
  hparams.dropout = 0.5
  hparams.add_hparam("num_compress_steps", 6)
  hparams.add_hparam("filter_double_steps", 2)
  hparams.add_hparam("video_modality_loss_cutoff", 0.02)
  return hparams


@registry.register_hparams
def next_frame_stochastic():
  """SV2P model."""
  hparams = common_hparams.basic_params1()
  hparams.batch_size = 8
  hparams.learning_rate_constant = 1e-3
  hparams.learning_rate_schedule = "constant"
  hparams.weight_decay = 0.0
  hparams.add_hparam("stochastic_model", True)
  hparams.add_hparam("latent_channels", 1)
  hparams.add_hparam("latent_std_min", -5.0)
  hparams.add_hparam("num_iterations_2nd_stage", 10000)
  hparams.add_hparam("latent_loss_multiplier", 1e-4)
  hparams.add_hparam("multi_latent", False)
  hparams.add_hparam("relu_shift", 1e-12)
  hparams.add_hparam("dna_kernel_size", 5)
  return hparams


@registry.register_hparams
def next_frame_tpu():
  hparams = next_frame()
  hparams.batch_size = 1


@registry.register_hparams
def next_frame_ae():
  """Conv autoencoder."""
  hparams = next_frame()
  hparams.input_modalities = "inputs:video:bitwise"
  hparams.hidden_size = 256
  hparams.batch_size = 8
  hparams.num_hidden_layers = 4
  hparams.num_compress_steps = 4
  hparams.dropout = 0.4
  return hparams


@registry.register_hparams
def next_frame_small():
  """Small conv model."""
  hparams = next_frame()
  hparams.hidden_size = 32
  return hparams


@registry.register_hparams
def next_frame_tiny():
  """Tiny for testing."""
  hparams = next_frame()
  hparams.hidden_size = 32
  hparams.num_hidden_layers = 1
  hparams.num_compress_steps = 2
  hparams.filter_double_steps = 1
  return hparams


@registry.register_hparams
def next_frame_l1():
  """Basic conv model with L1 modality."""
  hparams = next_frame()
  hparams.target_modality = "video:l1"
  hparams.video_modality_loss_cutoff = 2.4
  return hparams


@registry.register_hparams
def next_frame_l2():
  """Basic conv model with L2 modality."""
  hparams = next_frame()
  hparams.target_modality = "video:l2"
  hparams.video_modality_loss_cutoff = 2.4
  return hparams


@registry.register_ranged_hparams
def next_frame_base_range(rhp):
  """Basic tuning grid."""
  rhp.set_float("dropout", 0.2, 0.6)
  rhp.set_discrete("hidden_size", [64, 128, 256])
  rhp.set_int("num_compress_steps", 5, 8)
  rhp.set_discrete("batch_size", [4, 8, 16, 32])
  rhp.set_int("num_hidden_layers", 1, 3)
  rhp.set_int("filter_double_steps", 1, 6)
  rhp.set_float("learning_rate_constant", 1., 4.)
  rhp.set_int("learning_rate_warmup_steps", 500, 3000)
  rhp.set_float("initializer_gain", 0.8, 1.8)


@registry.register_ranged_hparams
def next_frame_doubling_range(rhp):
  """Filter doubling and dropout tuning grid."""
  rhp.set_float("dropout", 0.2, 0.6)
  rhp.set_int("filter_double_steps", 2, 5)


@registry.register_ranged_hparams
def next_frame_clipgrad_range(rhp):
  """Filter doubling and dropout tuning grid."""
  rhp.set_float("dropout", 0.3, 0.4)
  rhp.set_float("clip_grad_norm", 0.5, 10.0)


@registry.register_ranged_hparams
def next_frame_xent_cutoff_range(rhp):
  """Cross-entropy tuning grid."""
  rhp.set_float("video_modality_loss_cutoff", 0.005, 0.05)


@registry.register_ranged_hparams
def next_frame_ae_range(rhp):
  """Autoencoder world model tuning grid."""
  rhp.set_float("dropout", 0.3, 0.5)
  rhp.set_int("num_compress_steps", 1, 3)
  rhp.set_int("num_hidden_layers", 2, 6)
  rhp.set_float("learning_rate_constant", 1., 2.)
  rhp.set_float("initializer_gain", 0.8, 1.5)
  rhp.set_int("filter_double_steps", 2, 3)
