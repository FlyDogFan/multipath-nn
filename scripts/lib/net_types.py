from abc import ABCMeta
from functools import reduce
from types import SimpleNamespace as Ns

import numpy as np
import tensorflow as tf

from lib.layer_types import BatchNorm, Chain, Layer, LinTrans, NoOp, Rect

################################################################################
# Optimization
################################################################################

def params_list_rec(ℓ):
    if ℓ is not None:
        yield from vars(ℓ.params).values()
        for c in getattr(ℓ, 'comps', []):
            yield from params_list_rec(c)

def minimize_expected(net, cost, optimizer, α_rtr=1):
    lr_scales = {
        **{θ: 1 / tf.sqrt(tf.reduce_mean(tf.square(ℓ.p_tr)))
           for ℓ in net.layers for θ in params_list_rec(ℓ)},
        **{θ: α_rtr / tf.sqrt(tf.reduce_mean(tf.square(ℓ.p_tr)))
           for ℓ in net.layers for θ in params_list_rec(ℓ.router)}}
    grads = optimizer.compute_gradients(cost)
    scaled_grads = [(lr_scales[θ] * g, θ) for g, θ in grads if g is not None]
    return optimizer.apply_gradients(scaled_grads)

################################################################################
# Error Mapping
################################################################################

def add_error_mapping(net, ℓ):
    λ = net.hypers.λ_em
    ℓ.μ_tr = tf.Variable(0.0, trainable=False)
    ℓ.μ_vl = tf.Variable(0.0, trainable=False)
    ℓ.d_tr = tf.Variable(0.0, trainable=False)
    ℓ.d_vl = tf.Variable(0.0, trainable=False)
    μ_batch = tf.reduce_sum(ℓ.p_tr * ℓ.c_err) / tf.reduce_sum(ℓ.p_tr)
    ℓ.update_μ_tr = tf.group(
        tf.assign(ℓ.μ_tr, λ * ℓ.μ_tr + (1 - λ) * μ_batch),
        tf.assign(ℓ.d_tr, λ * ℓ.d_tr + 1 - λ))
    ℓ.update_μ_vl = tf.group(
        tf.assign(ℓ.μ_vl, λ * ℓ.μ_vl + (1 - λ) * μ_batch),
        tf.assign(ℓ.d_vl, λ * ℓ.d_vl + 1 - λ))
    ℓ.c_err_cor = tf.cond(
        ℓ.d_tr * ℓ.d_vl > 0,
        lambda: ℓ.c_err - ℓ.μ_tr / ℓ.d_tr + ℓ.μ_vl / ℓ.d_vl,
        lambda: ℓ.c_err)
    setattr(net.params, 'μ_tr_%i' % list(net.layers).index(ℓ), ℓ.μ_tr)
    setattr(net.params, 'μ_vl_%i' % list(net.layers).index(ℓ), ℓ.μ_vl)
    setattr(net.params, 'd_tr_%i' % list(net.layers).index(ℓ), ℓ.d_tr)
    setattr(net.params, 'd_vl_%i' % list(net.layers).index(ℓ), ℓ.d_vl)

################################################################################
# Root Network Class
################################################################################

class Net(metaclass=ABCMeta):
    default_hypers = Ns(x0_shape=(), y_shape=())

    def __init__(self, **options):
        self.root = options.pop('root', NoOp())
        self.hypers = Ns(**{**vars(type(self).default_hypers), **options})
        self.params = Ns()
        self.x0 = tf.placeholder(tf.float32, (None,) + self.hypers.x0_shape)
        self.y = tf.placeholder(tf.float32, (None,) + self.hypers.y_shape)
        self.mode = tf.placeholder_with_default('ev', ())
        self.train = tf.no_op()
        self.validate = tf.no_op()
        self.link()

    def link(self):
        def link_layer(ℓ, x, y, mode):
            ℓ.link(x, y, mode)
            if ℓ.router is not None:
                ℓ.router.link(ℓ.x, y, mode)
            for s in ℓ.sinks:
                link_layer(s, ℓ.x, y, mode)
        link_layer(self.root, self.x0, self.y, self.mode)

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
    def switches(self):
        return (ℓ for ℓ in self.layers if len(ℓ.sinks) > 1)

################################################################################
# Statically-Routed Networks
################################################################################

class SRNet(Net):
    default_hypers = Ns(λ_lrn=1e-3, μ_lrn=0.9)

    def link(self):
        super().link()
        ϕ = self.hypers
        self.λ_lrn = tf.placeholder_with_default(ϕ.λ_lrn, ())
        self.μ_lrn = tf.placeholder_with_default(ϕ.μ_lrn, ())
        for ℓ in self.layers:
            ℓ.p_ev = tf.ones((tf.shape(self.x0)[0],))
        c_tr = sum(ℓ.c_err + ℓ.c_mod for ℓ in self.layers)
        opt = tf.train.MomentumOptimizer(self.λ_lrn, self.μ_lrn)
        self.train = opt.minimize(tf.reduce_mean(c_tr))

################################################################################
# Decision Smoothing Networks
################################################################################

def route_sinks_ds_stat(ℓ, opts, net):
    for s in ℓ.sinks:
        route_ds(s, ℓ.p_tr, ℓ.p_ev, opts, net)

def route_sinks_ds_dyn(ℓ, opts, net):
    def n_leaves(ℓ): return (
        1 if len(ℓ.sinks) == 0
        else sum(map(n_leaves, ℓ.sinks)))
    w_struct = np.divide(list(map(n_leaves, ℓ.sinks)), n_leaves(ℓ))
    π_tr = (1 - opts.ϵ) * tf.nn.softmax(ℓ.router.x / opts.τ) + opts.ϵ * w_struct
    π_ev = tf.to_float(tf.equal(
        tf.expand_dims(tf.to_int32(tf.argmax(ℓ.router.x, 1)), 1),
        tf.range(len(ℓ.sinks))))
    for i, s in enumerate(ℓ.sinks):
        route_ds(s, ℓ.p_tr * π_tr[:, i], ℓ.p_ev * π_ev[:, i], opts, net)

def route_ds(ℓ, p_tr, p_ev, opts, net):
    ℓ.p_tr = p_tr
    ℓ.p_ev = p_ev
    add_error_mapping(net, ℓ)
    if len(ℓ.sinks) < 2: route_sinks_ds_stat(ℓ, opts, net)
    else: route_sinks_ds_dyn(ℓ, opts, net)

class DSNet(Net):
    default_hypers = Ns(
        k_cpt=0.0, ϵ=1e-3, τ=1.0, λ_em=0.9,
        λ_lrn=1e-3, μ_lrn=0.9, α_rtr=1.0)

    def link(self):
        super().link()
        ϕ = self.hypers
        self.λ_lrn = tf.placeholder_with_default(ϕ.λ_lrn, ())
        self.μ_lrn = tf.placeholder_with_default(ϕ.μ_lrn, ())
        self.ϵ = tf.placeholder_with_default(ϕ.ϵ, ())
        self.τ = tf.placeholder_with_default(ϕ.τ, ())
        n_pts = tf.shape(self.x0)[0]
        route_ds(self.root, tf.ones((n_pts,)), tf.ones((n_pts,)),
                 Ns(ϵ=self.ϵ, τ=self.τ), self)
        c_err = sum(ℓ.p_tr * ℓ.c_err_cor for ℓ in self.layers)
        c_cpt = sum(ℓ.p_tr * ϕ.k_cpt * (ℓ.n_ops + getattr(ℓ.router, 'n_ops', 0))
                    for ℓ in self.layers)
        c_mod = sum(tf.stop_gradient(ℓ.p_tr) * (ℓ.c_mod + ℓ.router.c_mod)
                    for ℓ in self.switches)
        c_tr = c_err + c_cpt + c_mod
        opt = tf.train.MomentumOptimizer(self.λ_lrn, self.μ_lrn)
        with tf.control_dependencies([ℓ.update_μ_tr for ℓ in self.layers]):
            self.train = minimize_expected(
                self, tf.reduce_mean(c_tr), opt, ϕ.α_rtr)
        self.validate = tf.group(*(ℓ.update_μ_vl for ℓ in self.layers))

################################################################################
# Actor Networks
################################################################################

def route_sinks_ac_stat(ℓ, opts, net):
    for s in ℓ.sinks:
        route_ac(s, ℓ.p_tr, ℓ.p_ev, opts, net)
    ℓ.c_ev = (
        ℓ.c_err + opts.k_cpt * ℓ.n_ops
        + sum(s.c_ev for s in ℓ.sinks))
    ℓ.c_opt = (
        ℓ.c_err + opts.k_cpt * ℓ.n_ops
        + sum(s.c_opt for s in ℓ.sinks))
    ℓ.c_rtr = 0

def route_sinks_ac_dyn(ℓ, opts, net):
    def n_leaves(ℓ): return (
        1 if len(ℓ.sinks) == 0
        else sum(map(n_leaves, ℓ.sinks)))
    w_struct = np.divide(list(map(n_leaves, ℓ.sinks)), n_leaves(ℓ))
    π_tr = (1 - opts.ϵ) * tf.nn.softmax(ℓ.router.x / opts.τ) + opts.ϵ * w_struct
    π_ev = tf.to_float(tf.equal(
        tf.expand_dims(tf.to_int32(tf.argmax(ℓ.router.x, 1)), 1),
        tf.range(len(ℓ.sinks))))
    for i, s in enumerate(ℓ.sinks):
        route_ac(s, ℓ.p_tr * π_tr[:, i], ℓ.p_ev * π_ev[:, i], opts, net)
    ℓ.c_ev = (
        ℓ.c_err + opts.k_cpt * (ℓ.n_ops + ℓ.router.n_ops)
        + sum(π_ev[:, i] * s.c_ev
              for i, s in enumerate(ℓ.sinks)))
    ℓ.c_opt = (
        ℓ.c_err + opts.k_cpt * (ℓ.n_ops + ℓ.router.n_ops)
        + reduce(tf.minimum, (s.c_opt for s in ℓ.sinks)))
    ℓ.c_rtr = (
        opts.k_rtr * sum(
            π_tr[:, i] * tf.stop_gradient(
                s.c_opt if opts.optimistic else s.c_ev)
            for i, s in enumerate(ℓ.sinks)))

def route_ac(ℓ, p_tr, p_ev, opts, net):
    ℓ.p_tr = p_tr
    ℓ.p_ev = p_ev
    add_error_mapping(net, ℓ)
    if len(ℓ.sinks) < 2: route_sinks_ac_stat(ℓ, opts, net)
    else: route_sinks_ac_dyn(ℓ, opts, net)

class AcNet(Net):
    default_hypers = Ns(
        k_cpt=0.0, k_rtr=1.0, ϵ=1e-3, τ=1.0,
        λ_em=0.9, λ_lrn=1e-3, μ_lrn=0.9,
        α_rtr=1.0, optimistic=False)

    def link(self):
        super().link()
        ϕ = self.hypers
        self.λ_lrn = tf.placeholder_with_default(ϕ.λ_lrn, ())
        self.μ_lrn = tf.placeholder_with_default(ϕ.μ_lrn, ())
        self.ϵ = tf.placeholder_with_default(ϕ.ϵ, ())
        self.τ = tf.placeholder_with_default(ϕ.τ, ())
        n_pts = tf.shape(self.x0)[0]
        route_ac(self.root, tf.ones((n_pts,)), tf.ones((n_pts,)),
                 Ns(ϵ=self.ϵ, τ=self.τ, k_cpt=ϕ.k_cpt, k_rtr=ϕ.k_rtr,
                    optimistic=ϕ.optimistic), self)
        c_err = sum(tf.stop_gradient(ℓ.p_tr) * ℓ.c_err for ℓ in self.layers)
        c_rtr = sum(tf.stop_gradient(ℓ.p_tr) * ℓ.c_rtr for ℓ in self.layers)
        c_mod = sum(tf.stop_gradient(ℓ.p_tr) * (ℓ.c_mod + ℓ.router.c_mod)
                    for ℓ in self.switches)
        c_tr = c_err + c_rtr + c_mod
        opt = tf.train.MomentumOptimizer(self.λ_lrn, self.μ_lrn)
        with tf.control_dependencies([ℓ.update_μ_tr for ℓ in self.layers]):
            self.train = minimize_expected(
                self, tf.reduce_mean(c_tr), opt, ϕ.α_rtr)
        self.validate = tf.group(*(ℓ.update_μ_vl for ℓ in self.layers))

################################################################################
# Cost Regression Networks
################################################################################

def route_sinks_cr_stat(ℓ, opts, net):
    for s in ℓ.sinks:
        route_cr(s, ℓ.p_tr, ℓ.p_ev, opts, net)
    ℓ.c_ev = (
        ℓ.c_err + opts.k_cpt * ℓ.n_ops
        + sum(s.c_ev for s in ℓ.sinks))
    ℓ.c_opt = (
        ℓ.c_err + opts.k_cpt * ℓ.n_ops
        + sum(s.c_opt for s in ℓ.sinks))
    ℓ.c_cre = 0

def route_sinks_cr_dyn(ℓ, opts, net):
    def n_leaves(ℓ): return (
        1 if len(ℓ.sinks) == 0
        else sum(map(n_leaves, ℓ.sinks)))
    w_struct = np.divide(list(map(n_leaves, ℓ.sinks)), n_leaves(ℓ))
    π_tr = (1 - opts.ϵ) * tf.nn.softmax(ℓ.router.x / opts.τ) + opts.ϵ * w_struct
    π_ev = tf.to_float(tf.equal(
        tf.expand_dims(tf.to_int32(tf.argmax(ℓ.router.x, 1)), 1),
        tf.range(len(ℓ.sinks))))
    for i, s in enumerate(ℓ.sinks):
        route_cr(s, ℓ.p_tr * π_tr[:, i], ℓ.p_ev * π_ev[:, i], opts, net)
    ℓ.c_ev = (
        ℓ.c_err + opts.k_cpt * (ℓ.n_ops + ℓ.router.n_ops)
        + sum(π_ev[:, i] * s.c_ev
              for i, s in enumerate(ℓ.sinks)))
    ℓ.c_opt = (
        ℓ.c_err + opts.k_cpt * (ℓ.n_ops + ℓ.router.n_ops)
        + reduce(tf.minimum, (s.c_opt for s in ℓ.sinks)))
    ℓ.c_cre = (
        opts.k_cre * sum(
            tf.square(ℓ.router.x[:, i] + tf.stop_gradient(
                s.c_opt if opts.optimistic else s.c_ev))
            for i, s in enumerate(ℓ.sinks)))

def route_cr(ℓ, p_tr, p_ev, opts, net):
    ℓ.p_tr = p_tr
    ℓ.p_ev = p_ev
    add_error_mapping(net, ℓ)
    if len(ℓ.sinks) < 2: route_sinks_cr_stat(ℓ, opts, net)
    else: route_sinks_cr_dyn(ℓ, opts, net)

class CRNet(Net):
    default_hypers = Ns(
        k_cpt=0.0, k_cre=1e-3, ϵ=1e-3, τ=0.01,
        λ_em=0.9, λ_lrn=1e-3, μ_lrn=0.9,
        α_rtr=1.0, optimistic=False)

    def link(self):
        super().link()
        ϕ = self.hypers
        self.λ_lrn = tf.placeholder_with_default(ϕ.λ_lrn, ())
        self.μ_lrn = tf.placeholder_with_default(ϕ.μ_lrn, ())
        self.ϵ = tf.placeholder_with_default(ϕ.ϵ, ())
        self.τ = tf.placeholder_with_default(ϕ.τ, ())
        n_pts = tf.shape(self.x0)[0]
        route_cr(self.root, tf.ones((n_pts,)), tf.ones((n_pts,)),
                 Ns(ϵ=self.ϵ, τ=self.τ, k_cpt=ϕ.k_cpt, k_cre=ϕ.k_cre,
                    optimistic=ϕ.optimistic), self)
        c_err = sum(tf.stop_gradient(ℓ.p_tr) * ℓ.c_err for ℓ in self.layers)
        c_cre = sum(tf.stop_gradient(ℓ.p_tr) * ℓ.c_cre for ℓ in self.layers)
        c_mod = sum(tf.stop_gradient(ℓ.p_tr) * (ℓ.c_mod + ℓ.router.c_mod)
                    for ℓ in self.switches)
        c_tr = c_err + c_cre + c_mod
        opt = tf.train.MomentumOptimizer(self.λ_lrn, self.μ_lrn)
        with tf.control_dependencies([ℓ.update_μ_tr for ℓ in self.layers]):
            self.train = minimize_expected(
                self, tf.reduce_mean(c_tr), opt, ϕ.α_rtr)
        self.validate = tf.group(*(ℓ.update_μ_vl for ℓ in self.layers))
