import numpy as np
import tensorflow as tf

################################################################################
# Network
################################################################################

class Net:
    def __init__(self, x0_shape, y_shape, root):
        self.x0 = tf.placeholder(tf.float32, (None,) + x0_shape)
        self.y = tf.placeholder(tf.float32, (None,) + y_shape)
        root.p_tr = tf.ones(tf.shape(self.x0)[:1])
        root.p_ev = tf.ones(tf.shape(self.x0)[:1])
        def link(layer, x):
            n_sinks = len(layer.sinks)
            layer.x = x
            layer.π_tr = tf.ones((tf.shape(x)[0], n_sinks)) / n_sinks
            layer.π_ev = tf.ones((tf.shape(x)[0], n_sinks)) / n_sinks
            layer.link_forward(x)
            for i, sink in enumerate(layer.sinks):
                sink.p_tr = layer.p_tr * layer.π_tr[:, i]
                sink.p_ev = layer.p_ev * layer.π_ev[:, i]
                link(sink, layer.x)
            layer.y_est = (
                layer.x if len(layer.sinks) == 0 else
                sum(s.y_est * tf.reshape(
                        layer.π_ev[:, i],
                        (-1,) + (s.y_est.get_shape().ndims - 1) * (1,))
                    for i, s in enumerate(layer.sinks)))
            layer.ℓ_loc = tf.zeros(tf.shape(x)[:1])
            layer.link_backward(self.y)
            layer.ℓ_tr = (
                layer.ℓ_loc +
                sum(layer.π_tr[:, i] * s.ℓ_tr
                    for i, s in enumerate(layer.sinks)))
            layer.ℓ_ev = (
                layer.ℓ_loc +
                sum(layer.π_ev[:, i] * s.ℓ_ev
                    for i, s in enumerate(layer.sinks)))
        link(root, self.x0)
        self.root = root
        self.y_est = root.y_est
        self.ℓ_tr = root.ℓ_tr
        self.ℓ_ev = root.ℓ_ev

    @property
    def layers(self):
        def all_in_tree(layer):
            yield layer
            for sink in layer.sinks:
                yield from all_in_tree(sink)
        yield from all_in_tree(self.root)

################################################################################
# Layers
################################################################################

# layer properties:
# - forward linking:
#   - x: output activity (default: input activity)
#   - π_tr: routing policy during training (default: uniform)
#   - π_ev: routing policy during evaluation (default: uniform)
# - backward linking:
#   - ℓ_loc: layer-local loss (default: 0)

class ReLin:
    def __init__(self, n_chan, k_cpt, k_l2, sink):
        self.n_chan = n_chan
        self.k_cpt = k_cpt
        self.k_l2 = k_l2
        self.sinks = [sink]

    def link_forward(self, x):
        n_chan_in = np.prod([d.value for d in x.get_shape()[1:]])
        x_flat = tf.reshape(x, (tf.shape(x)[0], n_chan_in))
        w_shape = (n_chan_in, self.n_chan)
        w_scale = 2 / np.sqrt(n_chan_in)
        self.w = tf.Variable(w_scale * tf.random_normal(w_shape))
        self.b = tf.Variable(tf.zeros(self.n_chan))
        self.x = tf.nn.relu(tf.matmul(x_flat, self.w) + self.b)

    def link_backward(self, y):
        self.ℓ_loc = (self.k_cpt * tf.to_float(tf.size(self.w))
                      + tf.ones((tf.shape(self.x)[0], 1))
                      * self.k_l2 * tf.reduce_sum(tf.square(self.w)))

class ReConv:
    def __init__(self, n_chan, step, supp, k_cpt, k_l2, sink):
        self.n_chan = n_chan
        self.step = step
        self.supp = supp
        self.k_cpt = k_cpt
        self.k_l2 = k_l2
        self.sinks = [sink]

    def link_forward(self, x):
        u = np.linspace(-2, 2, self.supp)[:, None, None, None]
        v = np.linspace(-2, 2, self.supp)[:, None, None]
        w_env = np.exp(-(u**2 - v**2) / 2) / np.sum(np.exp(-(u**2 - v**2) / 2))
        n_chan_in = x.get_shape()[3].value
        w_scale = w_env * np.sqrt(self.supp**2 / n_chan_in)
        w_shape = (self.supp, self.supp, n_chan_in, self.n_chan)
        steps = (1, self.step, self.step, 1)
        self.w = tf.Variable(w_scale * tf.random_normal(w_shape))
        self.b = tf.Variable(tf.zeros(self.n_chan))
        self.x = tf.nn.relu(tf.nn.conv2d(x, self.w, steps, 'SAME') + self.b)

    def link_backward(self, y):
        self.ℓ_loc = (
            self.k_cpt * tf.to_float(
                tf.size(self.w) * tf.shape(self.x)[1] * tf.shape(self.x)[2]
                / self.step**2)
            + tf.ones((tf.shape(self.x)[0], 1))
            * self.k_l2 * tf.reduce_sum(tf.square(self.w)))

class ReConvMP:
    def __init__(self, n_chan, step, supp, k_cpt, k_l2, sink):
        self.n_chan = n_chan
        self.step = step
        self.supp = supp
        self.k_cpt = k_cpt
        self.k_l2 = k_l2
        self.sinks = [sink]

    def link_forward(self, x):
        u = np.linspace(-2, 2, self.supp)[:, None, None, None]
        v = np.linspace(-2, 2, self.supp)[:, None, None]
        w_env = np.exp(-(u**2 - v**2) / 2) / np.sum(np.exp(-(u**2 - v**2) / 2))
        n_chan_in = x.get_shape()[3].value
        w_scale = w_env * np.sqrt(self.supp**2 / n_chan_in)
        w_shape = (self.supp, self.supp, n_chan_in, self.n_chan)
        self.w = tf.Variable(w_scale * tf.random_normal(w_shape))
        self.b = tf.Variable(tf.zeros(self.n_chan))
        self.x = tf.nn.max_pool(
            tf.nn.relu(tf.nn.conv2d(x, self.w, (1, 1, 1, 1), 'SAME') + self.b),
            (1, self.step, self.step, 1), (1, self.step, self.step, 1),
            'SAME')

    def link_backward(self, y):
        self.ℓ_loc = (
            self.k_cpt * tf.to_float(
                tf.size(self.w) * tf.shape(self.x)[1] * tf.shape(self.x)[2])
            + tf.ones((tf.shape(self.x)[0], 1))
            * self.k_l2 * tf.reduce_sum(tf.square(self.w)))

class LogReg:
    def __init__(self, n_classes, k_l2, ϵ=1e-6):
        self.n_classes = n_classes
        self.k_l2 = k_l2
        self.ϵ = ϵ
        self.sinks = []

    def link_forward(self, x):
        n_chan_in = np.prod([d.value for d in x.get_shape()[1:]])
        x_flat = tf.reshape(x, (tf.shape(x)[0], n_chan_in))
        w_shape = (n_chan_in, self.n_classes)
        w_scale = 1 / np.sqrt(n_chan_in)
        self.w = tf.Variable(w_scale * tf.random_normal(w_shape))
        self.b = tf.Variable(tf.zeros(self.n_classes))
        self.x = tf.nn.softmax(tf.matmul(x_flat, self.w) + self.b)

    def link_backward(self, y):
        self.ℓ_loc = (-tf.reduce_sum(y * tf.log(tf.maximum(self.ϵ, self.x)), 1)
                      + tf.ones((tf.shape(self.x)[0], 1))
                      * self.k_l2 * tf.reduce_sum(tf.square(self.w)))

class DSRouting:
    def __init__(self, ϵ, *sinks):
        self.ϵ = ϵ
        self.sinks = sinks

    def link_forward(self, x):
        n_chan_in = np.prod([d.value for d in x.get_shape()[1:]])
        x_flat = tf.reshape(x, (tf.shape(x)[0], n_chan_in))
        w_shape = (n_chan_in, len(self.sinks))
        self.w = tf.Variable(tf.random_normal(w_shape) / np.sqrt(n_chan_in))
        self.b = tf.Variable(tf.zeros(len(self.sinks)))
        self.π_tr = (
            self.ϵ / len(self.sinks)
            + (1 - self.ϵ) * tf.nn.softmax(tf.matmul(x_flat, self.w) + self.b))
        self.π_ev = tf.to_float(
            tf.equal(self.π_tr, tf.reduce_max(self.π_tr, 1, True)))

    def link_backward(self, y):
        pass

class CRRouting:
    def __init__(self, k_cre, ϵ, *sinks):
        self.k_cre = k_cre
        self.ϵ = ϵ
        self.sinks = sinks

    def link_forward(self, x):
        n_chan_in = np.prod([d.value for d in x.get_shape()[1:]])
        x_flat = tf.reshape(x, (tf.shape(x)[0], n_chan_in))
        w_shape = (n_chan_in, len(self.sinks))
        self.w = tf.Variable(tf.random_normal(w_shape) / np.sqrt(n_chan_in))
        self.b = tf.Variable(tf.zeros(len(self.sinks)))
        self.ℓ_est = tf.matmul(x_flat, self.w) + self.b
        self.π_tr = (
            self.ϵ / len(self.sinks)
            + (1 - self.ϵ) * tf.to_float(
                tf.equal(self.ℓ_est, tf.reduce_min(self.ℓ_est, 1, True))))
        self.π_ev = tf.to_float(
            tf.equal(self.ℓ_est, tf.reduce_min(self.ℓ_est, 1, True)))

    def link_backward(self, y):
        self.ℓ_loc = self.k_cre * sum(
            tf.square(self.sinks[i].ℓ_ev - self.ℓ_est[:, i])
            for i in range(len(self.sinks)))

# # Smart Routing (to-do: clean up)
#
# class DSRouting:
#     def __init__(self, ϵ, *sinks):
#         self.ϵ = ϵ
#         self.sinks = sinks
#
#     def link_forward(self, x):
#         n_chan_in = np.prod([d.value for d in x.get_shape()[1:]])
#         x_flat = tf.reshape(x, (tf.shape(x)[0], n_chan_in))
#         self.w0 = tf.Variable(tf.random_normal((n_chan_in, 16)) / np.sqrt(n_chan_in))
#         self.w1 = tf.Variable(tf.random_normal((16, len(self.sinks))) / 4)
#         self.b0 = tf.Variable(tf.zeros(16))
#         self.b1 = tf.Variable(tf.zeros(len(self.sinks)))
#         self.π_tr = (
#             self.ϵ / len(self.sinks) +
#             (1 - self.ϵ) *
#             tf.nn.softmax(
#                 tf.matmul(
#                     tf.nn.relu(tf.matmul(x_flat, self.w0) + self.b0),
#                     self.w1)
#                 + self.b1))
#         self.π_ev = tf.to_float(
#             tf.equal(self.π_tr, tf.reduce_max(self.π_tr, 1, True)))
#
#     def link_backward(self, y):
#         pass
