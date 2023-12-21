"""
Adjustment Algorithms
=====================

This file defines the different steps, to be wrapped into the Adjustment objects.
"""
from __future__ import annotations

import bottleneck as bn
import numpy as np
import xarray as xr
from xarray.core.utils import get_temp_dimname

from xclim.core.units import convert_units_to, infer_context, units
from xclim.indices.stats import _fitfunc_1d  # noqa

from . import nbutils as nbu
from . import utils as u
from ._processing import _adapt_freq, _reordering
from .base import Grouper, map_blocks, map_groups
from .detrending import PolyDetrend
from .processing import standardize

# from .processing import escore, standardize


def _adapt_freq_hist(ds, adapt_freq_thresh):
    """Adapt frequency of null values of `hist`    in order to match `ref`."""
    with units.context(infer_context(ds.ref.attrs.get("standard_name"))):
        thresh = convert_units_to(adapt_freq_thresh, ds.ref)
    dim = ["time"] + ["window"] * ("window" in ds.hist.dims)
    return _adapt_freq.func(
        xr.Dataset(dict(sim=ds.hist, ref=ds.ref)), thresh=thresh, dim=dim
    ).sim_ad


@map_groups(
    af=[Grouper.PROP, "quantiles"],
    hist_q=[Grouper.PROP, "quantiles"],
    scaling=[Grouper.PROP],
)
def dqm_train(ds, *, dim, kind, quantiles, adapt_freq_thresh) -> xr.Dataset:
    """Train step on one group.

    Notes
    -----
    Dataset must contain the following variables:
      ref : training target
      hist : training data

    adapt_freq_thresh : str | None
        Threshold for frequency adaptation. See :py:class:`xclim.sdba.processing.adapt_freq` for details.
        Default is None, meaning that frequency adaptation is not performed.
    """
    hist = _adapt_freq_hist(ds, adapt_freq_thresh) if adapt_freq_thresh else ds.hist

    refn = u.apply_correction(ds.ref, u.invert(ds.ref.mean(dim), kind), kind)
    histn = u.apply_correction(hist, u.invert(hist.mean(dim), kind), kind)

    ref_q = nbu.quantile(refn, quantiles, dim)
    hist_q = nbu.quantile(histn, quantiles, dim)

    af = u.get_correction(hist_q, ref_q, kind)
    mu_ref = ds.ref.mean(dim)
    mu_hist = hist.mean(dim)
    scaling = u.get_correction(mu_hist, mu_ref, kind=kind)

    return xr.Dataset(data_vars=dict(af=af, hist_q=hist_q, scaling=scaling))


@map_groups(
    af=[Grouper.PROP, "quantiles"],
    hist_q=[Grouper.PROP, "quantiles"],
)
def eqm_train(ds, *, dim, kind, quantiles, adapt_freq_thresh) -> xr.Dataset:
    """EQM: Train step on one group.

    Dataset variables:
      ref : training target
      hist : training data

    adapt_freq_thresh : str | None
        Threshold for frequency adaptation. See :py:class:`xclim.sdba.processing.adapt_freq` for details.
        Default is None, meaning that frequency adaptation is not performed.
    """
    hist = _adapt_freq_hist(ds, adapt_freq_thresh) if adapt_freq_thresh else ds.hist
    ref_q = nbu.quantile(ds.ref, quantiles, dim)
    hist_q = nbu.quantile(hist, quantiles, dim)

    af = u.get_correction(hist_q, ref_q, kind)

    return xr.Dataset(data_vars=dict(af=af, hist_q=hist_q))


# =======================================================================================
# general functions, should in utils or something
# =======================================================================================
def _rank(arr, axis=None):
    rnk = bn.nanrankdata(arr, axis=axis)
    rnk = rnk / np.nanmax(rnk, axis=axis, keepdims=True)
    mx, mn = 1, np.nanmin(rnk, axis=axis, keepdims=True)
    return mx * (rnk - mn) / (mx - mn)


def _get_group_complement(da, group):
    # complement of "dayofyear": "year", etc.
    gr = group.name if isinstance(group, Grouper) else group
    gr = group
    if gr == "time.dayofyear":
        return da.time.dt.year
    if gr == "time.month":
        return da.time.dt.strftime("%Y-%d")


def get_windowed_group(da, group, n_group_chunks=1, stack_dim=None):
    r"""Splits an input array into `group`, its complement, and expands the array along a rolling `window` dimension.

    Aims to give a faster alternative to `map_blocks` constructions.

    """
    # define dims (simplify this)
    if stack_dim is None:
        stack_dim = get_temp_dimname(da.dims, "stack_dim")
    win_dim = get_temp_dimname(da.dims, "window_dim")
    group = group if isinstance(group, Grouper) else Grouper(group, 1)
    gr, win = group.name, group.window
    gr_dim = gr.split(".")[-1]
    gr_complement_dim = get_temp_dimname(da.dims, "group_complement_dim")
    time_dims = [gr_dim, gr_complement_dim]
    complement_dims = [win_dim, gr_complement_dim]
    # should grouper allow time & win>1? I think only win=1 makes sense... Grouper should raise error
    if group.name == "time":
        da = da.rename({"time": stack_dim})
    else:
        if win == 1:
            da = da.expand_dims({win_dim: [0]})
        else:
            da = da.rolling(time=win, center=True).construct(window_dim=win_dim)
        da = da.groupby(gr).apply(
            lambda da: da.assign_coords(time=_get_group_complement(da, gr)).rename(
                {"time": gr_complement_dim}
            )
        )
        da = da.chunk({gr_dim: n_group_chunks, gr_complement_dim: -1})
        da = da.stack({stack_dim: complement_dims})

    da = da.assign_attrs(
        {
            "grouping": {
                "group": (gr, win),
                "complement_dims": complement_dims,
                "stack_dim": stack_dim,
                "time_dims": time_dims,
                "window_dim": win_dim,
            }
        }
    )
    return da


def ungroup(gr_da, template_time):
    r"""Inverse the operation done with :py:func:`get_windowed_group`."""
    group = Grouper(*gr_da.attrs["grouping"]["group"])
    # group = group if isinstance(group, Grouper) else Grouper(group, 1)
    stack_dim, win_dim, time_dims = (
        gr_da.attrs["grouping"][lab] for lab in ["stack_dim", "window_dim", "time_dims"]
    )
    if group.name == "time":
        return gr_da.rename({stack_dim: "time"})
    gr_da = gr_da.unstack(stack_dim)
    pos_center = gr_da[win_dim].size // 2
    gr_da = gr_da[{win_dim: slice(pos_center, pos_center + 1)}]
    grouped_time = get_windowed_group(
        template_time[{d: 0 for d in template_time.dims if d != "time"}], group.name
    )
    grouped_time = grouped_time.unstack(stack_dim)
    da = (
        gr_da.stack(time=time_dims)
        .drop_vars(time_dims)
        .assign_coords(time=grouped_time.values.ravel())
    )
    return da.where(da.time.notnull(), drop=True)[{win_dim: 0}]


# =======================================================================================
# train/adjust functions for npdf
# =======================================================================================
def _npdft(ref, hist, rots, quantiles, method, extrap):
    af_q = np.zeros((len(rots), ref.shape[0], len(quantiles)))
    for ii in range(len(rots)):
        rot = rots[0] if ii == 0 else rots[ii] @ rots[ii - 1].T
        ref, hist = (rot @ da for da in [ref, hist])
        for iv in range(ref.shape[0]):
            ref_q, hist_q = (
                nbu._sortquantile(da, quantiles) for da in [ref[iv], hist[iv]]
            )
            af_q[ii, iv] = u.get_correction(hist_q, ref_q, "+")
            if iv < ref.shape[0] - 1:
                af = u._interp_on_quantiles_1D(
                    _rank(hist[iv]),
                    quantiles,
                    af_q[ii, iv],
                    method=method,
                    extrap=extrap,
                )
                hist[iv] = u.apply_correction(hist[iv], af, "+")
    return af_q


# weird git bug, useless comment to have a new commit
def npdf_train(
    ds,
    rot_matrices,
    quantiles,
    g_idxs,
    gw_idxs,
    method,
    extrap,
    n_escore,
) -> xr.Dataset:
    """EQM: Train step on one group.


    I was thinking we should leave the possibility to standardize outside the function. Given it should
    really be done in each group, once a group is formed, it would not make sense to allow to standardize outside.
    The only use for this is: If we let group=None a possibility, then simply specifying the dim along which to perform the computation
    should be sufficient.

    Parameters
    ----------
    Dataset variables:
        ref : training target
        hist : training data
    n_iter : int
        The number of iterations to perform. Defaults to 20.
    pts_dim : str
        The name of the "multivariate" dimension. Defaults to "multivar", which is the
        normal case when using :py:func:`xclim.sdba.base.stack_variables`.
    rot_matrices : xr.DataArray, optional
        The rotation matrices as a 3D array ('iterations', <pts_dim>, <anything>), with shape (n_iter, <N>, <N>).
        If left empty, random rotation matrices will be automatically generated.
    base_kws : dict, optional
        Arguments passed to the training of the univariate adjustment.
    adj_kws : dict, optional
        Dictionary of arguments to pass to the adjust method of the univariate adjustment.
    standarize_inplace : bool
        If true, perform a standardization of ref,hist,sim. Defaults to false
    """
    # unload data
    ref = ds.ref
    hist = ds.hist
    # npdf core
    ref, hist = (standardize(da, dim="time")[0] for da in [ref, hist])
    gr_dim = gw_idxs.attrs["group_dim"]
    af_q_l = []
    for ib in range(gw_idxs[gr_dim].size):
        indices = gw_idxs[{gr_dim: ib}].astype(int).values
        af_q = xr.apply_ufunc(
            _npdft,
            standardize(ref[{"time": indices[indices >= 0]}], dim="time")[0],
            standardize(hist[{"time": indices[indices >= 0]}], dim="time")[0],
            rot_matrices,
            quantiles,
            input_core_dims=[
                ["multivar", "time"],
                ["multivar", "time"],
                ["iterations", "multivar_prime", "multivar"],
                ["quantiles"],
            ],
            output_core_dims=[
                # ["multivar", dim],
                ["iterations", "multivar_prime", "quantiles"],
            ],
            dask="parallelized",
            output_dtypes=[ref.dtype],
            # output_sizes={"quantiles": len(quantiles)},
            kwargs={"method": method, "extrap": extrap},
            vectorize=True,
        )
        af_q_l.append(af_q.expand_dims({gr_dim: [ib]}))
    af_q = xr.concat(af_q_l, dim=gr_dim).assign_coords(
        {"quantiles": quantiles, gr_dim: gw_idxs[gr_dim].values}
    )
    return xr.Dataset(dict(af_q=af_q))  # , scenh_npdft=scenh_npdft))


def npdf_adjust(
    scen,
    sim,
    ds,
    group,
    n_group_chunks,
    method,
    extrap,
    period_dim,
) -> xr.Dataset:
    """Npdf adjust

    * for now, I had to keep sim separated from ds, had a weird scalar window_dim remaining in sim else
    * period_dim is used to indicate we want to compute many sim periods at once. If specified, it should be

    the dimension generated with stack_periods. unstacking will be done outside the function.



    adapt_freq_thresh : str | None
        Threshold for frequency adaptation. See :py:class:`xclim.sdba.processing.adapt_freq` for details.
        Default is None, meaning that frequency adaptation is not performed.
    """
    # unload training parameters
    rots = ds.rot_matrices
    af_q = ds.af_q
    quantiles = af_q.quantiles

    # group and standardize
    gr_sim = get_windowed_group(sim, group, n_group_chunks=n_group_chunks)
    gr_scen = get_windowed_group(scen, group, n_group_chunks=n_group_chunks)
    gr_scen_attrs = gr_scen.attrs
    dim = gr_scen_attrs["grouping"]["stack_dim"]
    dims = [dim] if period_dim is None else [period_dim, dim]
    gr_sim = standardize(gr_sim, dim=dim)[0]
    # npdf core (adjust)
    for i_it in range(rots.iterations.size):
        R = rots.isel(iterations=i_it, drop=True)
        # rotate
        simp = gr_sim @ R

        # adjust
        rnks = xr.apply_ufunc(
            _rank,
            simp,
            input_core_dims=[[dim]],
            output_core_dims=[[dim]],
            dask="parallelized",
            vectorize=True,
        )
        af = xr.apply_ufunc(
            u._interp_on_quantiles_1D_multi,
            rnks,
            quantiles,
            af_q.isel(iterations=i_it, drop=True),
            input_core_dims=[dims, ["quantiles"], ["quantiles"]],
            output_core_dims=[dims],
            dask="parallelized",
            kwargs=dict(method=method, extrap=extrap),
            output_dtypes=[gr_sim.dtype],
            vectorize=True,
        )
        simp = u.apply_correction(simp, af, "+")

        # undo rotation
        gr_sim = simp @ R

    # reordering
    gr_scen_reordered = _reordering.func(
        xr.Dataset({"ref": gr_sim, "sim": gr_scen}), dim=dim
    ).reordered
    # undo grouping
    scen_reordered = ungroup(gr_scen_reordered.assign_attrs(gr_scen_attrs), scen.time)
    return scen_reordered.to_dataset(name="scen")


@map_blocks(reduces=[Grouper.PROP, "quantiles"], scen=[])
def qm_adjust(ds, *, group, interp, extrapolation, kind) -> xr.Dataset:
    """QM (DQM and EQM): Adjust step on one block.

    Dataset variables:
      af : Adjustment factors
      hist_q : Quantiles over the training data
      sim : Data to adjust.
    """
    af = u.interp_on_quantiles(
        ds.sim,
        ds.hist_q,
        ds.af,
        group=group,
        method=interp,
        extrapolation=extrapolation,
    )

    scen = u.apply_correction(ds.sim, af, kind).rename("scen")
    return scen.to_dataset()


@map_blocks(reduces=[Grouper.PROP, "quantiles"], scen=[], trend=[])
def dqm_adjust(ds, *, group, interp, kind, extrapolation, detrend):
    """DQM adjustment on one block.

    Dataset variables:
      scaling : Scaling factor between ref and hist
      af : Adjustment factors
      hist_q : Quantiles over the training data
      sim : Data to adjust
    """
    scaled_sim = u.apply_correction(
        ds.sim,
        u.broadcast(
            ds.scaling,
            ds.sim,
            group=group,
            interp=interp if group.prop != "dayofyear" else "nearest",
        ),
        kind,
    )

    if isinstance(detrend, int):
        detrend = PolyDetrend(degree=detrend, kind=kind, group=group)

    detrend = detrend.fit(scaled_sim)
    ds["sim"] = detrend.detrend(scaled_sim)
    scen = qm_adjust.func(
        ds,
        group=group,
        interp=interp,
        extrapolation=extrapolation,
        kind=kind,
    ).scen
    scen = detrend.retrend(scen)

    out = xr.Dataset({"scen": scen, "trend": detrend.ds.trend})
    return out


@map_blocks(reduces=[Grouper.PROP, "quantiles"], scen=[], sim_q=[])
def qdm_adjust(ds, *, group, interp, extrapolation, kind) -> xr.Dataset:
    """QDM: Adjust process on one block.

    Dataset variables:
      af : Adjustment factors
      hist_q : Quantiles over the training data
      sim : Data to adjust.
    """
    sim_q = group.apply(u.rank, ds.sim, main_only=True, pct=True)
    af = u.interp_on_quantiles(
        sim_q,
        ds.quantiles,
        ds.af,
        group=group,
        method=interp,
        extrapolation=extrapolation,
    )
    scen = u.apply_correction(ds.sim, af, kind)
    return xr.Dataset(dict(scen=scen, sim_q=sim_q))


@map_blocks(
    reduces=[Grouper.ADD_DIMS, Grouper.DIM],
    af=[Grouper.PROP],
    hist_thresh=[Grouper.PROP],
)
def loci_train(ds, *, group, thresh) -> xr.Dataset:
    """LOCI: Train on one block.

    Dataset variables:
      ref : training target
      hist : training data
    """
    s_thresh = group.apply(
        u.map_cdf, ds.rename(hist="x", ref="y"), y_value=thresh
    ).isel(x=0)
    sth = u.broadcast(s_thresh, ds.hist, group=group)
    ws = xr.where(ds.hist >= sth, ds.hist, np.nan)
    wo = xr.where(ds.ref >= thresh, ds.ref, np.nan)

    ms = group.apply("mean", ws, skipna=True)
    mo = group.apply("mean", wo, skipna=True)

    # Adjustment factor
    af = u.get_correction(ms - s_thresh, mo - thresh, u.MULTIPLICATIVE)
    return xr.Dataset({"af": af, "hist_thresh": s_thresh})


@map_blocks(reduces=[Grouper.PROP], scen=[])
def loci_adjust(ds, *, group, thresh, interp) -> xr.Dataset:
    """LOCI: Adjust on one block.

    Dataset variables:
      hist_thresh : Hist's equivalent thresh from ref
      sim : Data to adjust
    """
    sth = u.broadcast(ds.hist_thresh, ds.sim, group=group, interp=interp)
    factor = u.broadcast(ds.af, ds.sim, group=group, interp=interp)
    with xr.set_options(keep_attrs=True):
        scen = (factor * (ds.sim - sth) + thresh).clip(min=0)
    return scen.rename("scen").to_dataset()


@map_groups(af=[Grouper.PROP])
def scaling_train(ds, *, dim, kind) -> xr.Dataset:
    """Scaling: Train on one group.

    Dataset variables:
      ref : training target
      hist : training data
    """
    mhist = ds.hist.mean(dim)
    mref = ds.ref.mean(dim)
    af = u.get_correction(mhist, mref, kind)
    return af.rename("af").to_dataset()


@map_blocks(reduces=[Grouper.PROP], scen=[])
def scaling_adjust(ds, *, group, interp, kind) -> xr.Dataset:
    """Scaling: Adjust on one block.

    Dataset variables:
      af: Adjustment factors.
      sim : Data to adjust.
    """
    af = u.broadcast(ds.af, ds.sim, group=group, interp=interp)
    scen = u.apply_correction(ds.sim, af, kind)
    return scen.rename("scen").to_dataset()


def _fit_on_cluster(data, thresh, dist, cluster_thresh):
    """Extract clusters on 1D data and fit "dist" on the maximums."""
    _, _, _, maximums = u.get_clusters_1d(data, thresh, cluster_thresh)
    params = list(
        _fitfunc_1d(maximums - thresh, dist=dist, floc=0, nparams=3, method="ML")
    )
    # We forced 0, put back thresh.
    params[-2] = thresh
    return params


def _extremes_train_1d(ref, hist, ref_params, *, q_thresh, cluster_thresh, dist, N):
    """Train for method ExtremeValues, only for 1D input along time."""
    # Find quantile q_thresh
    thresh = (
        np.quantile(ref[ref >= cluster_thresh], q_thresh)
        + np.quantile(hist[hist >= cluster_thresh], q_thresh)
    ) / 2

    # Fit genpareto on cluster maximums on ref (if needed) and hist.
    if np.isnan(ref_params).all():
        ref_params = _fit_on_cluster(ref, thresh, dist, cluster_thresh)

    hist_params = _fit_on_cluster(hist, thresh, dist, cluster_thresh)

    # Find probabilities of extremes according to fitted dist
    Px_ref = dist.cdf(ref[ref >= thresh], *ref_params)
    hist = hist[hist >= thresh]
    Px_hist = dist.cdf(hist, *hist_params)

    # Find common probabilities range.
    Pmax = min(Px_ref.max(), Px_hist.max())
    Pmin = max(Px_ref.min(), Px_hist.min())
    Pcommon = (Px_hist <= Pmax) & (Px_hist >= Pmin)
    Px_hist = Px_hist[Pcommon]

    # Find values of hist extremes if they followed ref's distribution.
    hist_in_ref = dist.ppf(Px_hist, *ref_params)

    # Adjustment factors, unsorted
    af = hist_in_ref / hist[Pcommon]
    # sort them in Px order, and pad to have N values.
    order = np.argsort(Px_hist)
    px_hist = np.pad(Px_hist[order], ((0, N - af.size),), constant_values=np.NaN)
    af = np.pad(af[order], ((0, N - af.size),), constant_values=np.NaN)

    return px_hist, af, thresh


@map_blocks(
    reduces=["time"], px_hist=["quantiles"], af=["quantiles"], thresh=[Grouper.PROP]
)
def extremes_train(ds, *, group, q_thresh, cluster_thresh, dist, quantiles):
    """Train extremes for a given variable series."""
    px_hist, af, thresh = xr.apply_ufunc(
        _extremes_train_1d,
        ds.ref,
        ds.hist,
        ds.ref_params or np.NaN,
        input_core_dims=[("time",), ("time",), ()],
        output_core_dims=[("quantiles",), ("quantiles",), ()],
        vectorize=True,
        kwargs={
            "q_thresh": q_thresh,
            "cluster_thresh": cluster_thresh,
            "dist": dist,
            "N": len(quantiles),
        },
    )
    # Outputs of map_blocks must have dimensions.
    if not isinstance(thresh, xr.DataArray):
        thresh = xr.DataArray(thresh)
    thresh = thresh.expand_dims(group=[1])
    return xr.Dataset(
        {"px_hist": px_hist, "af": af, "thresh": thresh},
        coords={"quantiles": quantiles},
    )


def _fit_cluster_and_cdf(data, thresh, dist, cluster_thresh):
    """Fit 1D cluster maximums and immediately compute CDF."""
    fut_params = _fit_on_cluster(data, thresh, dist, cluster_thresh)
    return dist.cdf(data, *fut_params)


@map_blocks(reduces=["quantiles", Grouper.PROP], scen=[])
def extremes_adjust(
    ds, *, group, frac, power, dist, interp, extrapolation, cluster_thresh
):
    """Adjust extremes to reflect many distribution factors."""
    # Find probabilities of extremes of fut according to its own cluster-fitted dist.
    px_fut = xr.apply_ufunc(
        _fit_cluster_and_cdf,
        ds.sim,
        ds.thresh,
        input_core_dims=[["time"], []],
        output_core_dims=[["time"]],
        kwargs={"dist": dist, "cluster_thresh": cluster_thresh},
        vectorize=True,
    )

    # Find factors by interpolating from hist probs to fut probs. apply them.
    af = u.interp_on_quantiles(
        px_fut, ds.px_hist, ds.af, method=interp, extrapolation=extrapolation
    )
    scen = u.apply_correction(ds.sim, af, "*")

    # Smooth transition function between simulation and scenario.
    transition = (
        ((ds.sim - ds.thresh) / ((ds.sim.max("time")) - ds.thresh)) / frac
    ) ** power
    transition = transition.clip(0, 1)

    out = (transition * scen) + ((1 - transition) * ds.scen)
    return out.rename("scen").squeeze("group", drop=True).to_dataset()
