"""Loss functions for training models."""
import copy
import json
import os
import warnings

import numpy as np
import tensorflow as tf

from cleverhans.attacks import Attack
from cleverhans.compat import softmax_cross_entropy_with_logits
from cleverhans.model import Model
from cleverhans.utils import safe_zip

try:
  import tensorflow_probability as tfp
  tf_distributions = tfp.distributions
except ImportError:
  tf_distributions = tf.distributions


class Loss(object):
  """
  An abstract interface for loss wrappers that allows flexible control of
  real examples, adversarial examples and labels. These losses are used
  for defenses (during model training).
  """

  def __init__(self, model, hparams=None, attack=None):
    """
    :param model: Model instance, the model on which to apply the loss.
    :param hparams: dict, hyper-parameters for the loss.
    :param attack: cleverhans.attacks.Attack instance
    """
    assert isinstance(model, Model)
    standard = attack is None or isinstance(attack, Attack)
    deprecated = callable(attack)
    if not standard and not deprecated:
      raise TypeError("`attack` must be `None` or `Attack` subclass instance")
    if deprecated:
      warnings.warn("callable attacks are deprecated, switch to an Attack "
                    "subclass. callable attacks will not be supported after "
                    "2019-05-05.")
      class Wrapper(Attack):
        """
        Temporary wrapper class to be removed when deprecated callable
        arguments are removed.

        :param f: a callable object implementing the attack
        """
        def __init__(self, f):
          dummy_model = Model()
          super(Wrapper, self).__init__(model=dummy_model)
          self.f = f

        def generate(self, x):
          return self.f(x)

      attack = Wrapper(attack)
    self.model = model
    self.hparams = hparams
    self.attack = attack

  def save(self, path):
    """Save loss in json format
    """
    json.dump(dict(loss=self.__class__.__name__,
                   params=self.hparams),
              open(os.path.join(path, 'loss.json'), 'wb'))

  def fprop(self, x, y):
    """Forward propagate the loss.
    Loss should be a scalar value, independent of batch size (i.e. use
    reduce_mean over batch axis, don't use reduce_sum or return a tensor).
    Scalar losses are easier to add together, e.g. through `WeightedSum`.
    Mean losses are easier to redistribute across multiple replicas without
    needing to change learning rates, etc.
    :param x: tensor, a batch of inputs.
    :param y: tensor, a batch of outputs (1-hot labels typically).
    """
    raise NotImplementedError


class WeightedSum(Loss):
  """
  A Loss that adds up a weighted sum of other losses.
  """

  def __init__(self, model, terms):
    self.terms = terms

    Loss.__init__(self, model, locals())

  def fprop(self, x, y, **kwargs):
    weights, loss_objects = safe_zip(*self.terms)
    for weight in weights:
      if isinstance(weight, float):
        continue
      if hasattr(weight, 'ndim'):
        assert weight.ndim == 0
        continue
      raise TypeError("weight of %s is not a type that this function "
                      "knows it can accept yet" % str(weight))
    losses = [loss.fprop(x, y, **kwargs) for loss in loss_objects]
    for loss, loss_object in safe_zip(losses, loss_objects):
      if len(loss.get_shape()) > 0:
        raise ValueError("%s.fprop returned a non-scalar value" %
                         str(loss_object))
    terms = [weight * loss for weight, loss in safe_zip(weights, losses)]

    return tf.add_n(terms)


class CrossEntropy(Loss):
  """Cross-entropy loss for a multiclass softmax classifier.
  :param model: Model instance, the model on which to apply the loss.
  :param smoothing: float, amount of label smoothing for cross-entropy.
  :param attack: function, given an input x, return an attacked x'.
  :param pass_y: bool, if True pass y to the attack
  :param adv_coeff: Coefficient to put on the cross-entropy for
    adversarial examples, if adversarial examples are used.
    The coefficient on the cross-entropy for clean examples is
    1. - adv_coeff.
  :param attack_params: dict, keyword arguments passed to `attack.generate`
  """
  def __init__(self, model, smoothing=0., attack=None, pass_y=False,
               adv_coeff=0.5, attack_params=None,
               **kwargs):
    if smoothing < 0 or smoothing > 1:
      raise ValueError('Smoothing must be in [0, 1]', smoothing)
    self.kwargs = kwargs
    Loss.__init__(self, model, locals(), attack)
    self.smoothing = smoothing
    self.adv_coeff = adv_coeff
    self.pass_y = pass_y
    self.attack_params = attack_params

  def fprop(self, x, y, **kwargs):
    kwargs.update(self.kwargs)
    if self.attack is not None:
      attack_params = copy.copy(self.attack_params)
      if attack_params is None:
        attack_params = {}
      if self.pass_y:
        attack_params['y'] = y
      x = x, self.attack.generate(x, **attack_params)
      coeffs = [1. - self.adv_coeff, self.adv_coeff]
      if self.adv_coeff == 1.:
        x = (x[1],)
        coeffs = (coeffs[1],)
    else:
      x = tuple([x])
      coeffs = [1.]
    assert np.allclose(sum(coeffs), 1.)

    # Catching RuntimeError: Variable -= value not supported by tf.eager.
    try:
      y -= self.smoothing * (y - 1. / tf.cast(y.shape[-1], y.dtype))
    except RuntimeError:
      y.assign_sub(self.smoothing * (y - 1. / tf.cast(y.shape[-1],
                                                      y.dtype)))

    logits = [self.model.get_logits(x, **kwargs) for x in x]
    loss = sum(
        coeff * tf.reduce_mean(softmax_cross_entropy_with_logits(labels=y,
                                                                 logits=logit))
        for coeff, logit in safe_zip(coeffs, logits))
    return loss


class MixUp(Loss):
  """Mixup ( https://arxiv.org/abs/1710.09412 )
  :param model: Model instance, the model on which to apply the loss.
  :param beta: float, beta distribution parameter for MixUp.
  """
  def __init__(self, model, beta, **kwargs):
    del kwargs
    Loss.__init__(self, model, locals())
    self.beta = beta

  def fprop(self, x, y, **kwargs):
    with tf.device('/CPU:0'):
      # Prevent error complaining GPU kernels unavailable for this.
      mix = tf_distributions.Beta(self.beta, self.beta)
      mix = mix.sample([tf.shape(x)[0]] + [1] * (len(x.shape) - 1))
    mix = tf.maximum(mix, 1 - mix)
    mix_label = tf.reshape(mix, [-1, 1])
    xm = x + mix * (x[::-1] - x)
    ym = y + mix_label * (y[::-1] - y)
    logits = self.model.get_logits(xm, **kwargs)
    loss = tf.reduce_mean(softmax_cross_entropy_with_logits(labels=ym,
                                                            logits=logits))
    return loss


class FeaturePairing(Loss):
  """Feature pairing loss.
  :param model: Model instance, the model on which to apply the loss.
  :param weight: float, with of logic pairing loss.
  :param attack: function, given an input x, return an attacked x'.
  """

  def __init__(self, model, weight, attack, **kwargs):
    del kwargs
    Loss.__init__(self, model, locals(), attack)
    self.weight = weight

  def fprop(self, x, y, **kwargs):
    x_adv = self.attack.generate(x)
    d1 = self.model.fprop(x, **kwargs)
    d2 = self.model.fprop(x_adv, **kwargs)
    pairing_loss = [tf.reduce_mean(tf.square(a - b))
                    for a, b in
                    zip(d1[Model.O_FEATURES], d2[Model.O_FEATURES])]
    pairing_loss = tf.reduce_mean(pairing_loss)
    loss = tf.reduce_mean(softmax_cross_entropy_with_logits(
        labels=y, logits=d1[Model.O_LOGITS]))
    loss += tf.reduce_mean(softmax_cross_entropy_with_logits(
        labels=y, logits=d2[Model.O_LOGITS]))
    return loss + self.weight * pairing_loss


class WeightDecay(Loss):
  """Weight decay"""
  def fprop(self, x, y, **kwargs):
    terms = [tf.nn.l2_loss(param)
             for param in self.model.get_params()
             if len(param.get_shape()) > 1]
    out = tf.add_n(terms)
    assert len(out.get_shape()) == 0
    return out


class LossCrossEntropy(Loss):
  """
  Deprecated version of `CrossEntropy` that returns per-example loss rather
  than mean loss.
  """

  def __init__(self, model, smoothing=0., attack=None, **kwargs):
    """Constructor.
    :param model: Model instance, the model on which to apply the loss.
    :param smoothing: float, amount of label smoothing for cross-entropy.
    :param attack: function, given an input x, return an attacked x'.
    """
    if smoothing < 0 or smoothing > 1:
      raise ValueError('Smoothing must be in [0, 1]', smoothing)
    del kwargs
    Loss.__init__(self, model, locals(), attack)
    self.smoothing = smoothing

  def fprop(self, x, y, **kwargs):
    if self.attack is not None:
      x = x, self.attack(x)
    else:
      x = tuple([x])

    # Catching RuntimeError: Variable -= value not supported by tf.eager.
    try:
      y -= self.smoothing * (y - 1. / tf.cast(y.shape[-1], tf.float32))
    except RuntimeError:
      y.assign_sub(self.smoothing * (y - 1. / tf.cast(y.shape[-1],
                                                      tf.float32)))

    logits = [self.model.get_logits(x, **kwargs) for x in x]
    loss = sum(
        softmax_cross_entropy_with_logits(labels=y,
                                          logits=logit)
        for logit in logits)
    warnings.warn("LossCrossEntropy is deprecated, switch to "
                  "CrossEntropy. LossCrossEntropy may be removed on "
                  "or after 2019-03-06.")
    return loss


class LossFeaturePairing(Loss):
  """Deprecated version of `FeaturePairing` that returns per-example loss
  rather than mean loss."""

  def __init__(self, model, weight, attack, **kwargs):
    """Constructor.
    :param model: Model instance, the model on which to apply the loss.
    :param weight: float, with of logic pairing loss.
    :param attack: function, given an input x, return an attacked x'.
    """
    del kwargs
    Loss.__init__(self, model, locals(), attack)
    self.weight = weight

  def fprop(self, x, y, **kwargs):
    x_adv = self.attack(x)
    d1 = self.model.fprop(x, **kwargs)
    d2 = self.model.fprop(x_adv, **kwargs)
    pairing_loss = [tf.reduce_mean(tf.square(a - b))
                    for a, b in
                    zip(d1[Model.O_FEATURES], d2[Model.O_FEATURES])]
    pairing_loss = tf.reduce_mean(pairing_loss)
    loss = softmax_cross_entropy_with_logits(
        labels=y, logits=d1[Model.O_LOGITS])
    loss += softmax_cross_entropy_with_logits(
        labels=y, logits=d2[Model.O_LOGITS])
    warnings.warn("LossFeaturePairing is deprecated, switch to "
                  "FeaturePairing. LossFeaturePairing may be removed "
                  "on or after 2019-03-06.")
    return loss + self.weight * pairing_loss


class LossMixUp(Loss):
  """Deprecated version of `MixUp` that returns per-example loss
  rather than mean loss."""

  def __init__(self, model, beta, **kwargs):
    """Constructor.
    :param model: Model instance, the model on which to apply the loss.
    :param beta: float, beta distribution parameter for MixUp.
    """
    del kwargs
    Loss.__init__(self, model, locals())
    self.beta = beta

  def fprop(self, x, y, **kwargs):
    mix = tf_distributions.Beta(self.beta, self.beta)
    mix = mix.sample([tf.shape(x)[0]] + [1] * (len(x.shape) - 1))
    xm = x + mix * (x[::-1] - x)
    ym = y + mix * (y[::-1] - y)
    logits = self.model.get_logits(xm, **kwargs)
    loss = softmax_cross_entropy_with_logits(labels=ym, logits=logits)
    warnings.warn("LossMixUp is deprecated, switch to "
                  "MixUp. LossFeaturePairing may be removed "
                  "on or after 2019-03-06.")
    return loss


class SNNLCrossEntropy(CrossEntropy):
  EPSILON = 0.00001
  def __init__(self,
              model,
              temperature=100.,
              layer_names=[],
              factor=-10.,
              optimize_temperature = True,
              cos_distance = False):
    CrossEntropy.__init__(self, model, smoothing=0.)
    self.temperature = temperature
    self.factor = factor
    self.optimize_temperature = optimize_temperature
    self.cos_distance = cos_distance
    self.layer_names = layer_names
    if self.layer_names == []:
      #omit the final classification layer
      self.layer_names = model.get_layer_names()[:-1]

  @staticmethod
  def pairwise_euclid_distance(A,B):
    batchA = tf.shape(A)[0]
    batchB = tf.shape(B)[0]

    sqr_norm_A = tf.reshape(tf.reduce_sum(tf.pow(A, 2), 1), [1, batchA])
    sqr_norm_B = tf.reshape(tf.reduce_sum(tf.pow(B, 2), 1), [batchB, 1])
    inner_prod = tf.matmul(B, A, transpose_b=True)

    tile_1 = tf.tile(sqr_norm_A, [batchB, 1])
    tile_2 = tf.tile(sqr_norm_B, [1, batchA])
    return (tile_1 + tile_2 - 2 * inner_prod)

  @staticmethod
  def pairwise_cos_distance(A,B):
    normalized_A = tf.nn.l2_normalize(A, dim = 1)
    normalized_B = tf.nn.l2_normalize(B, dim = 1)
    prod = tf.matmul(normalized_A, normalized_B,adjoint_b = True)
    return 1 - prod

  @staticmethod
  def fits(A, B, temp,cos_distance):
    if cos_distance:
      distance_matrix = SNNLCrossEntropy.pairwise_cos_distance(A, B)
    else:
      distance_matrix = SNNLCrossEntropy.pairwise_euclid_distance(A, B)
    return tf.exp(-(distance_matrix /temp))

  @staticmethod
  def pick_probability(x, temp, cos_distance):
    f = SNNLCrossEntropy.fits(x, x, temp, cos_distance) - tf.eye(tf.shape(x)[0])
    return f / (
        SNNLCrossEntropy.EPSILON + tf.expand_dims(tf.reduce_sum(f, 1), 1))

  @staticmethod
  def same_label_mask(y, y2):
    return tf.cast(tf.squeeze(tf.equal(y, tf.expand_dims(y2, 1))), tf.float32)

  @staticmethod
  def masked_pick_probability(x, y, temp, cos_distance):
    return SNNLCrossEntropy.pick_probability(x, temp,  cos_distance) * \
              SNNLCrossEntropy.same_label_mask(y, y)

  @staticmethod
  def SNNL(x, y, temp, cos_distance):
    summed_masked_pick_prob = tf.reduce_sum(
      SNNLCrossEntropy.masked_pick_probability(x, y, temp, cos_distance), 1)
    return tf.reduce_mean(
        -tf.log(SNNLCrossEntropy.EPSILON + summed_masked_pick_prob))

  @staticmethod
  def optimizied_temp_SNNL(x, y, initial_temp, cos_distance):
    t = tf.Variable(1,dtype=tf.float32,trainable=False,name="temp")
    # we use inverse_temp because it seems to be more stable when optimizing
    def inverse_temp(t):
      return tf.div(initial_temp,t)
    ent_loss =  SNNLCrossEntropy.SNNL(x, y, inverse_temp(t), cos_distance)
    updated_t = tf.assign(t,tf.subtract(t,0.1*tf.gradients(ent_loss,t)[0]))
    inverse_t = inverse_temp(updated_t)
    return SNNLCrossEntropy.SNNL(x, y, inverse_t, cos_distance)


  def fprop(self, x, y, **kwargs):
    cross_entropy = CrossEntropy.fprop(self, x, y, **kwargs)
    self.layers = [self.model.get_layer(x, name) for name in self.layer_names]
    loss_fn = self.SNNL
    if self.optimize_temperature:
      loss_fn = self.SNNL
    layers_SNNL = [loss_fn(tf.layers.flatten(layer),
                          tf.argmax(y, axis=1),
                          self.temperature,
                          self.cos_distance)
                   for layer in self.layers]
    return cross_entropy + self.factor * tf.add_n(layers_SNNL)
