from abc import ABCMeta
from functools import reduce
from types import SimpleNamespace as Namespace

import numpy as np
import tensorflow as tf

from lib.layer_types import BatchNorm, Chain, Layer, LinTrans, Rect

################################################################################
# Optimization
################################################################################

def minimize_expected(net, cost, optimizer):
    lr_scales = {
        θ: 1 / tf.sqrt(tf.reduce_mean(tf.square(ℓ.p_tr)))
        for ℓ in net.layers for θ in [
            *vars(ℓ.router.params).values(),
            *vars(ℓ.params).values()]}
    grads = optimizer.compute_gradients(cost)
    scaled_grads = [(lr_scales[θ] * g, θ) for g, θ in grads if g is not None]
    return optimizer.apply_gradients(scaled_grads)

################################################################################
# Error Mapping
################################################################################

def add_error_mapping(ℓ, λ, ϵ=1e-3):
    ℓ.μ_tr = tf.Variable(0.0, trainable=False)
    ℓ.μ_vl = tf.Variable(0.0, trainable=False)
    ℓ.v_tr = tf.Variable(1.0, trainable=False)
    ℓ.v_vl = tf.Variable(1.0, trainable=False)
    μ_batch = (
        tf.reduce_sum(ℓ.p_tr * ℓ.c_err)
        / tf.reduce_sum(ℓ.p_tr))
    v_batch = (
        tf.reduce_sum(ℓ.p_tr * tf.square(ℓ.c_err - μ_batch))
        / tf.reduce_sum(ℓ.p_tr))
    ℓ.update_μv_tr = tf.group(
        tf.assign(ℓ.μ_tr, λ * ℓ.μ_tr + (1 - λ) * μ_batch),
        tf.assign(ℓ.v_tr, λ * ℓ.v_tr + (1 - λ) * v_batch))
    ℓ.update_μv_vl = tf.group(
        tf.assign(ℓ.μ_vl, λ * ℓ.μ_vl + (1 - λ) * μ_batch),
        tf.assign(ℓ.v_vl, λ * ℓ.v_vl + (1 - λ) * v_batch))
    ℓ.c_err_cor = (
        tf.sqrt((ℓ.v_vl + ϵ) / (ℓ.v_tr + ϵ))
        * (ℓ.c_err - ℓ.μ_tr) + ℓ.μ_vl)

################################################################################
# Root Network Class
################################################################################

def head(layer_tree):
    return layer_tree if isinstance(layer_tree, Layer) else layer_tree[0]

def tail(layer_tree):
    return [] if isinstance(layer_tree, Layer) else layer_tree[1:]

def link(layer_tree, x, y, mode):
    source, sinks = head(layer_tree), tail(layer_tree)
    source.link(x, y, mode)
    source.sinks = list(map(head, sinks))
    for s in sinks:
        link(s, source.x, y, mode)

class Net(metaclass=ABCMeta):
    def __init__(self, x0_shape, y_shape, layers):
        self.x0 = tf.placeholder(tf.float32, (None,) + x0_shape)
        self.y = tf.placeholder(tf.float32, (None,) + y_shape)
        self.mode = tf.placeholder_with_default('ev', ())
        self.root = head(layers)
        link(layers, self.x0, self.y, self.mode)
        self.train_op = tf.no_op()
        self.validate_op = tf.no_op()
        self.sess = tf.Session()

    def __del__(self):
        if hasattr(self, 'sess'):
            self.sess.close()

    @property
    def layers(self):
        def all_in_tree(layer):
            yield layer
            for sink in layer.sinks:
                yield from all_in_tree(sink)
        yield from all_in_tree(self.root)

    @property
    def leaves(self):
        return (ℓ for ℓ in self.layers if len(ℓ.sinks) == 0)

    @property
    def params(self):
        return Namespace(**{
            ('layer%i_%s' % (i, k)): v
            for i, ℓ in enumerate(self.layers)
            for k, v in vars(ℓ.params).items()})

    def write(self, path):
        saver = tf.train.Saver(vars(self.params))
        saver.save(self.sess, path, write_meta_graph=False)

    def read(self, path):
        saver = tf.train.Saver(vars(self.params))
        saver.restore(self.sess, path)

    def train(self, x0, y, hypers={}):
        self.sess.run(self.train_op, {
            self.x0: x0, self.y: y, self.mode: 'tr', **hypers})

    def validate(self, x0, y, hypers={}):
        self.sess.run(self.validate_op, {
            self.x0: x0, self.y: y, **hypers})

    def eval(self, target, x0, y, hypers={}):
        return self.sess.run(target, {
            self.x0: x0, self.y: y, **hypers})

################################################################################
# Statically-Routed Networks
################################################################################

class SRNet(Net):
    def __init__(self, x0_shape, y_shape, layers):
        super().__init__(x0_shape, y_shape, layers)
        ϕ = self.hypers = Namespace(
            λ_lrn=tf.placeholder_with_default(1e-3, ()),
            μ_lrn=tf.placeholder_with_default(0.9, ()))
        for ℓ in self.layers:
            ℓ.p_ev = tf.ones((tf.shape(ℓ.x)[0],))
        c_tr = sum(ℓ.c_err + ℓ.c_mod for ℓ in self.layers)
        opt = tf.train.MomentumOptimizer(ϕ.λ_lrn, ϕ.μ_lrn)
        self.train_op = opt.minimize(tf.reduce_mean(c_tr))
        self.sess.run(tf.initialize_all_variables())

################################################################################
# Decision Smoothing Networks
################################################################################

def route_sinks_ds_stat(ℓ, opts):
    ℓ.router = Chain()
    ℓ.router.link(ℓ.x, None, opts.mode)
    for s in ℓ.sinks:
        route_ds(s, ℓ.p_tr, ℓ.p_ev, opts)

def route_sinks_ds_dyn(ℓ, opts):
    ℓ.router = opts.router_gen(ℓ)
    ℓ.router.link(ℓ.x, None, opts.mode)
    def n_leaves(ℓ): return (
        1 if len(ℓ.sinks) == 0
        else sum(map(n_leaves, ℓ.sinks)))
    w_struct = np.divide(list(map(n_leaves, ℓ.sinks)), n_leaves(ℓ))
    π_tr = ((1 - opts.ϵ) * tf.nn.softmax(ℓ.router.x / opts.τ + np.log(w_struct))
            + opts.ϵ * w_struct)
    π_ev = tf.to_float(tf.equal(
        tf.expand_dims(tf.to_int32(tf.argmax(π_tr, 1)), 1),
        tf.range(len(ℓ.sinks))))
    for i, s in enumerate(ℓ.sinks):
        route_ds(s, ℓ.p_tr * π_tr[:, i], ℓ.p_ev * π_ev[:, i], opts)

def route_ds(ℓ, p_tr, p_ev, opts):
    ℓ.p_tr = p_tr
    ℓ.p_ev = p_ev
    add_error_mapping(ℓ, opts.λ_em)
    if len(ℓ.sinks) < 2: route_sinks_ds_stat(ℓ, opts)
    else: route_sinks_ds_dyn(ℓ, opts)

class DSNet(Net):
    def __init__(self, x0_shape, y_shape, router_gen, root):
        super().__init__(x0_shape, y_shape, root)
        ϕ = self.hypers = Namespace(
            k_cpt=tf.placeholder_with_default(0.0, ()),
            ϵ=tf.placeholder_with_default(0.1, ()),
            τ=tf.placeholder_with_default(1.0, ()),
            λ_em=tf.placeholder_with_default(0.9, ()),
            λ_lrn=tf.placeholder_with_default(1e-3, ()),
            μ_lrn=tf.placeholder_with_default(0.9, ()))
        n_pts = tf.shape(self.x0)[0]
        route_ds(self.root, tf.ones((n_pts,)), tf.ones((n_pts,)),
                 Namespace(router_gen=router_gen, mode=self.mode, **vars(ϕ)))
        c_err = sum(ℓ.p_tr * ℓ.c_err_cor for ℓ in self.layers)
        c_cpt = sum(ℓ.p_tr * ϕ.k_cpt * ℓ.n_ops for ℓ in self.layers)
        c_mod = sum(tf.stop_gradient(ℓ.p_tr) * (ℓ.c_mod + ℓ.router.c_mod)
                    for ℓ in self.layers)
        c_tr = c_err + c_cpt + c_mod
        opt = tf.train.MomentumOptimizer(ϕ.λ_lrn, ϕ.μ_lrn)
        with tf.control_dependencies([ℓ.update_μv_tr for ℓ in self.layers]):
            self.train_op = minimize_expected(self, tf.reduce_mean(c_tr), opt)
        self.validate_op = tf.group(*(ℓ.update_μv_vl for ℓ in self.layers))
        self.sess.run(tf.initialize_all_variables())

    @property
    def params(self):
        result = super().params
        for i, ℓ in enumerate(self.layers):
            setattr(result, 'μ_tr%i' % i, ℓ.μ_tr)
            setattr(result, 'μ_vl%i' % i, ℓ.μ_vl)
            setattr(result, 'v_tr%i' % i, ℓ.v_tr)
            setattr(result, 'v_vl%i' % i, ℓ.v_vl)
            for k, v in vars(ℓ.router.params).items():
                setattr(result, 'router%i_%s' % (i, k), v)
        return result

################################################################################
# Cost Regression Networks
################################################################################

def route_sinks_cr_stat(ℓ, opts):
    ℓ.router = Chain()
    ℓ.router.link(ℓ.x, None, opts.mode)
    for s in ℓ.sinks:
        route_cr(s, ℓ.p_tr, ℓ.p_ev, opts)
    ℓ.c_ev = (
        ℓ.c_err + opts.k_cpt * ℓ.n_ops
        + sum(s.c_ev for s in ℓ.sinks))
    ℓ.c_opt = (
        ℓ.c_err + opts.k_cpt * ℓ.n_ops
        + sum(s.c_opt for s in ℓ.sinks))
    ℓ.c_cre = 0.0

def route_sinks_cr_dyn(ℓ, opts):
    ℓ.router = opts.router_gen(ℓ)
    ℓ.router.link(ℓ.x, None, opts.mode)
    def n_leaves(ℓ): return (
        1 if len(ℓ.sinks) == 0
        else sum(map(n_leaves, ℓ.sinks)))
    w_struct = np.divide(list(map(n_leaves, ℓ.sinks)), n_leaves(ℓ))
    π_ev = tf.to_float(tf.equal(
        tf.expand_dims(tf.to_int32(tf.argmin(ℓ.router.x, 1)), 1),
        tf.range(len(ℓ.sinks))))
    π_tr = opts.ϵ * w_struct + (1 - opts.ϵ) * π_ev
    for i, s in enumerate(ℓ.sinks):
        route_cr(s, ℓ.p_tr * π_tr[:, i], ℓ.p_ev * π_ev[:, i], opts)
    ℓ.c_ev = (
        ℓ.c_err + opts.k_cpt * ℓ.n_ops
        + sum(π_ev[:, i] * s.c_ev
              for i, s in enumerate(ℓ.sinks)))
    ℓ.c_opt = (
        ℓ.c_err + opts.k_cpt * ℓ.n_ops
        + reduce(tf.minimum, (s.c_opt for s in ℓ.sinks)))
    ℓ.c_cre = (
        opts.k_cre * sum(
            π_tr[:, i] * tf.square(
                ℓ.router.x[:, i] - tf.stop_gradient(
                    s.c_opt if opts.optimistic else s.c_ev))
            for i, s in enumerate(ℓ.sinks)))

def route_cr(ℓ, p_tr, p_ev, opts):
    ℓ.p_tr = p_tr
    ℓ.p_ev = p_ev
    add_error_mapping(ℓ, opts.λ_em)
    if len(ℓ.sinks) < 2: route_sinks_cr_stat(ℓ, opts)
    else: route_sinks_cr_dyn(ℓ, opts)

class CRNet(Net):
    def __init__(self, x0_shape, y_shape, router_gen, optimistic, root):
        super().__init__(x0_shape, y_shape, root)
        ϕ = self.hypers = Namespace(
            k_cpt=tf.placeholder_with_default(0.0, ()),
            k_cre=tf.placeholder_with_default(1e-3, ()),
            ϵ=tf.placeholder_with_default(0.1, ()),
            λ_em=tf.placeholder_with_default(0.9, ()),
            λ_lrn=tf.placeholder_with_default(1e-3, ()),
            μ_lrn=tf.placeholder_with_default(0.9, ()))
        n_pts = tf.shape(self.x0)[0]
        route_cr(self.root, tf.ones((n_pts,)), tf.ones((n_pts,)),
                 Namespace(router_gen=router_gen, optimistic=optimistic,
                           mode=self.mode, **vars(ϕ)))
        c_err = sum(ℓ.p_tr * ℓ.c_err_cor for ℓ in self.layers)
        c_cpt = sum(ℓ.p_tr * ϕ.k_cpt * ℓ.n_ops for ℓ in self.layers)
        c_cre = sum(ℓ.p_tr * ℓ.c_cre for ℓ in self.layers)
        c_mod = sum(ℓ.p_tr * (ℓ.c_mod + ℓ.router.c_mod) for ℓ in self.layers)
        c_tr = c_err + c_cpt + c_cre + c_mod
        opt = tf.train.MomentumOptimizer(ϕ.λ_lrn, ϕ.μ_lrn)
        with tf.control_dependencies([ℓ.update_μv_tr for ℓ in self.layers]):
            self.train_op = minimize_expected(self, tf.reduce_mean(c_tr), opt)
        self.validate_op = tf.group(*(ℓ.update_μv_vl for ℓ in self.layers))
        self.sess.run(tf.initialize_all_variables())

    @property
    def params(self):
        result = super().params
        for i, ℓ in enumerate(self.layers):
            setattr(result, 'μ_tr%i' % i, ℓ.μ_tr)
            setattr(result, 'μ_vl%i' % i, ℓ.μ_vl)
            setattr(result, 'v_tr%i' % i, ℓ.v_tr)
            setattr(result, 'v_vl%i' % i, ℓ.v_vl)
            for k, v in vars(ℓ.router.params).items():
                setattr(result, 'router%i_%s' % (i, k), v)
        return result
