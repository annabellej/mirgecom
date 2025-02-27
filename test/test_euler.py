"""Test the Euler gas dynamics module."""

__copyright__ = """
Copyright (C) 2020 University of Illinois Board of Trustees
"""

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

import numpy as np
import numpy.random
import numpy.linalg as la  # noqa
import pyopencl.clmath  # noqa
import logging
import pytest
import math
from functools import partial

from pytools.obj_array import (
    flat_obj_array,
    make_obj_array,
)

from meshmode.dof_array import thaw
from meshmode.mesh import BTAG_ALL, BTAG_NONE  # noqa
from grudge.eager import interior_trace_pair
from grudge.symbolic.primitives import TracePair
from mirgecom.euler import euler_operator
from mirgecom.fluid import (
    split_conserved,
    join_conserved,
    make_conserved
)
from mirgecom.initializers import Vortex2D, Lump, MulticomponentLump
from mirgecom.boundary import PrescribedBoundary, DummyBoundary
from mirgecom.eos import IdealSingleGas
from grudge.eager import EagerDGDiscretization
from meshmode.array_context import (  # noqa
    pytest_generate_tests_for_pyopencl_array_context
    as pytest_generate_tests)


from grudge.shortcuts import make_visualizer
from mirgecom.inviscid import (
    get_inviscid_timestep,
    inviscid_flux
)

from mirgecom.integrators import rk4_step

logger = logging.getLogger(__name__)


@pytest.mark.parametrize("nspecies", [0, 1, 10])
@pytest.mark.parametrize("dim", [1, 2, 3])
def test_inviscid_flux(actx_factory, nspecies, dim):
    """Identity test - directly check inviscid flux routine
    :func:`mirgecom.inviscid.inviscid_flux` against the exact expected result.
    This test is designed to fail if the flux routine is broken.

    The expected inviscid flux is:
      F(q) = <rhoV, (E+p)V, rho(V.x.V) + pI, rhoY V>
    """
    actx = actx_factory()

    nel_1d = 16

    from meshmode.mesh.generation import generate_regular_rect_mesh

    #    for dim in [1, 2, 3]:
    mesh = generate_regular_rect_mesh(
        a=(-0.5,) * dim, b=(0.5,) * dim, nelements_per_axis=(nel_1d,) * dim
    )

    order = 3
    discr = EagerDGDiscretization(actx, mesh, order=order)
    eos = IdealSingleGas()

    logger.info(f"Number of {dim}d elems: {mesh.nelements}")

    def rand():
        from meshmode.dof_array import DOFArray
        return DOFArray(
            actx,
            tuple(actx.from_numpy(np.random.rand(grp.nelements, grp.nunit_dofs))
                  for grp in discr.discr_from_dd("vol").groups)
        )

    mass = rand()
    energy = rand()
    mom = make_obj_array([rand() for _ in range(dim)])

    mass_fractions = make_obj_array([rand() for _ in range(nspecies)])
    species_mass = mass * mass_fractions

    q = join_conserved(dim, mass=mass, energy=energy, momentum=mom,
                       species_mass=species_mass)
    cv = split_conserved(dim, q)

    # {{{ create the expected result

    p = eos.pressure(cv)
    escale = (energy + p) / mass

    numeq = dim + 2 + nspecies

    expected_flux = np.zeros((numeq, dim), dtype=object)
    expected_flux[0] = mom
    expected_flux[1] = mom * escale

    for i in range(dim):
        for j in range(dim):
            expected_flux[2+i, j] = (mom[i] * mom[j] / mass + (p if i == j else 0))

    for i in range(nspecies):
        expected_flux[dim+2+i] = mom * mass_fractions[i]

    expected_flux = split_conserved(dim, expected_flux)

    # }}}

    flux = inviscid_flux(discr, eos, cv)
    flux_resid = flux - expected_flux

    for i in range(numeq, dim):
        for j in range(dim):
            assert (la.norm(flux_resid[i, j].get())) == 0.0


@pytest.mark.parametrize("dim", [1, 2, 3])
def test_inviscid_flux_components(actx_factory, dim):
    """Test uniform pressure case.

    Checks that the Euler-internal inviscid flux routine
    :func:`mirgecom.inviscid.inviscid_flux` returns exactly the expected result
    with a constant pressure and no flow.

    Expected inviscid flux is:
      F(q) = <rhoV, (E+p)V, rho(V.x.V) + pI>

    Checks that only diagonal terms of the momentum flux:
      [ rho(V.x.V) + pI ] are non-zero and return the correctly calculated p.
    """
    actx = actx_factory()

    eos = IdealSingleGas()

    p0 = 1.0

    nel_1d = 4

    from meshmode.mesh.generation import generate_regular_rect_mesh
    mesh = generate_regular_rect_mesh(
        a=(-0.5,) * dim, b=(0.5,) * dim, nelements_per_axis=(nel_1d,) * dim
    )

    order = 3
    discr = EagerDGDiscretization(actx, mesh, order=order)
    eos = IdealSingleGas()

    logger.info(f"Number of {dim}d elems: {mesh.nelements}")
    # === this next block tests 1,2,3 dimensions,
    # with single and multiple nodes/states. The
    # purpose of this block is to ensure that when
    # all components of V = 0, the flux recovers
    # the expected values (and p0 within tolerance)
    # === with V = 0, fixed P = p0
    tolerance = 1e-15
    nodes = thaw(actx, discr.nodes())
    mass = discr.zeros(actx) + np.dot(nodes, nodes) + 1.0
    mom = make_obj_array([discr.zeros(actx) for _ in range(dim)])
    p_exact = discr.zeros(actx) + p0
    energy = p_exact / 0.4 + 0.5 * np.dot(mom, mom) / mass
    cv = make_conserved(dim, mass=mass, energy=energy, momentum=mom)
    p = eos.pressure(cv)
    flux = inviscid_flux(discr, eos, cv)
    assert discr.norm(p - p_exact, np.inf) < tolerance
    logger.info(f"{dim}d flux = {flux}")

    # for velocity zero, these components should be == zero
    assert discr.norm(flux.mass, 2) == 0.0
    assert discr.norm(flux.energy, 2) == 0.0

    # The momentum diagonal should be p
    # Off-diagonal should be identically 0
    assert discr.norm(flux.momentum - p0*np.identity(dim), np.inf) < tolerance


@pytest.mark.parametrize(("dim", "livedim"), [
    (1, 0),
    (2, 0),
    (2, 1),
    (3, 0),
    (3, 1),
    (3, 2),
    ])
def test_inviscid_mom_flux_components(actx_factory, dim, livedim):
    r"""Constant pressure, V != 0:

    Checks that the flux terms are returned in the proper order by running
    only 1 non-zero velocity component at-a-time.
    """
    actx = actx_factory()

    eos = IdealSingleGas()

    p0 = 1.0

    nel_1d = 4

    from meshmode.mesh.generation import generate_regular_rect_mesh
    mesh = generate_regular_rect_mesh(
        a=(-0.5,) * dim, b=(0.5,) * dim, nelements_per_axis=(nel_1d,) * dim
    )

    order = 3
    discr = EagerDGDiscretization(actx, mesh, order=order)
    nodes = thaw(actx, discr.nodes())

    tolerance = 1e-15
    for livedim in range(dim):
        mass = discr.zeros(actx) + 1.0 + np.dot(nodes, nodes)
        mom = make_obj_array([discr.zeros(actx) for _ in range(dim)])
        mom[livedim] = mass
        p_exact = discr.zeros(actx) + p0
        energy = (
            p_exact / (eos.gamma() - 1.0)
            + 0.5 * np.dot(mom, mom) / mass
        )
        cv = make_conserved(dim, mass=mass, energy=energy, momentum=mom)
        p = eos.pressure(cv)
        assert discr.norm(p - p_exact, np.inf) < tolerance
        flux = inviscid_flux(discr, eos, cv)
        logger.info(f"{dim}d flux = {flux}")
        vel_exact = mom / mass

        # first two components should be nonzero in livedim only
        assert discr.norm(flux.mass - mom, np.inf) == 0
        eflux_exact = (energy + p_exact)*vel_exact
        assert discr.norm(flux.energy - eflux_exact, np.inf) == 0

        logger.info("Testing momentum")
        xpmomflux = mass*np.outer(vel_exact, vel_exact) + p_exact*np.identity(dim)
        assert discr.norm(flux.momentum - xpmomflux, np.inf) < tolerance


@pytest.mark.parametrize("nspecies", [0, 10])
@pytest.mark.parametrize("order", [1, 2, 3])
@pytest.mark.parametrize("dim", [1, 2, 3])
def test_facial_flux(actx_factory, nspecies, order, dim):
    """Check the flux across element faces by prescribing states (q)
    with known fluxes. Only uniform states are tested currently - ensuring
    that the Lax-Friedrichs flux terms which are proportional to jumps in
    state data vanish.

    Since the returned fluxes use state data which has been interpolated
    to-and-from the element faces, this test is grid-dependent.
    """
    actx = actx_factory()

    tolerance = 1e-14
    p0 = 1.0

    from meshmode.mesh.generation import generate_regular_rect_mesh
    from pytools.convergence import EOCRecorder

    eoc_rec0 = EOCRecorder()
    eoc_rec1 = EOCRecorder()
    for nel_1d in [4, 8, 12]:

        mesh = generate_regular_rect_mesh(
            a=(-0.5,) * dim, b=(0.5,) * dim, nelements_per_axis=(nel_1d,) * dim
        )

        logger.info(f"Number of elements: {mesh.nelements}")

        discr = EagerDGDiscretization(actx, mesh, order=order)
        zeros = discr.zeros(actx)
        ones = zeros + 1.0

        mass_input = discr.zeros(actx) + 1.0
        energy_input = discr.zeros(actx) + 2.5
        mom_input = flat_obj_array(
            [discr.zeros(actx) for i in range(discr.dim)]
        )
        mass_frac_input = flat_obj_array(
            [ones / ((i + 1) * 10) for i in range(nspecies)]
        )
        species_mass_input = mass_input * mass_frac_input

        cv = make_conserved(
            dim, mass=mass_input, energy=energy_input, momentum=mom_input,
            species_mass=species_mass_input)

        from mirgecom.euler import _facial_flux

        # Check the boundary facial fluxes as called on an interior boundary
        interior_face_flux = _facial_flux(
            discr, eos=IdealSingleGas(), cv_tpair=interior_trace_pair(discr, cv))

        def inf_norm(data):
            if len(data) > 0:
                return discr.norm(data, np.inf, dd="all_faces")
            else:
                return 0.0

        # iff_split = split_conserved(dim, interior_face_flux)
        assert inf_norm(interior_face_flux.mass) < tolerance
        assert inf_norm(interior_face_flux.energy) < tolerance
        assert inf_norm(interior_face_flux.species_mass) < tolerance

        # The expected pressure is 1.0 (by design). And the flux diagonal is
        # [rhov_x*v_x + p] (etc) since we have zero velocities it's just p.
        #
        # The off-diagonals are zero. We get a {ndim}-vector for each
        # dimension, the flux for the x-component of momentum (for example) is:
        # f_momx = < 1.0, 0 , 0> , then we return f_momx .dot. normal, which
        # can introduce negative values.
        #
        # (Explanation courtesy of Mike Campbell,
        # https://github.com/illinois-ceesd/mirgecom/pull/44#discussion_r463304292)

        nhat = thaw(actx, discr.normal("int_faces"))
        mom_flux_exact = discr.project("int_faces", "all_faces", p0*nhat)
        print(f"{mom_flux_exact=}")
        print(f"{interior_face_flux.momentum=}")
        momerr = inf_norm(interior_face_flux.momentum - mom_flux_exact)
        assert momerr < tolerance
        eoc_rec0.add_data_point(1.0 / nel_1d, momerr)

        # Check the boundary facial fluxes as called on a domain boundary
        dir_mass = discr.project("vol", BTAG_ALL, mass_input)
        dir_e = discr.project("vol", BTAG_ALL, energy_input)
        dir_mom = discr.project("vol", BTAG_ALL, mom_input)
        dir_mf = discr.project("vol", BTAG_ALL, species_mass_input)

        dir_bval = make_conserved(dim, mass=dir_mass, energy=dir_e,
                                  momentum=dir_mom, species_mass=dir_mf)
        dir_bc = make_conserved(dim, mass=dir_mass, energy=dir_e,
                                momentum=dir_mom, species_mass=dir_mf)

        boundary_flux = _facial_flux(
            discr, eos=IdealSingleGas(),
            cv_tpair=TracePair(BTAG_ALL, interior=dir_bval, exterior=dir_bc)
        )

        assert inf_norm(boundary_flux.mass) < tolerance
        assert inf_norm(boundary_flux.energy) < tolerance
        assert inf_norm(boundary_flux.species_mass) < tolerance

        nhat = thaw(actx, discr.normal(BTAG_ALL))
        mom_flux_exact = discr.project(BTAG_ALL, "all_faces", p0*nhat)
        momerr = inf_norm(boundary_flux.momentum - mom_flux_exact)
        assert momerr < tolerance

        eoc_rec1.add_data_point(1.0 / nel_1d, momerr)

    logger.info(
        f"standalone Errors:\n{eoc_rec0}"
        f"boundary Errors:\n{eoc_rec1}"
    )
    assert (
        eoc_rec0.order_estimate() >= order - 0.5
        or eoc_rec0.max_error() < 1e-9
    )
    assert (
        eoc_rec1.order_estimate() >= order - 0.5
        or eoc_rec1.max_error() < 1e-9
    )


@pytest.mark.parametrize("nspecies", [0, 10])
@pytest.mark.parametrize("dim", [1, 2, 3])
@pytest.mark.parametrize("order", [1, 2, 3])
def test_uniform_rhs(actx_factory, nspecies, dim, order):
    """Tests the inviscid rhs using a trivial constant/uniform state which
    should yield rhs = 0 to FP.  The test is performed for 1, 2, and 3 dimensions.
    """
    actx = actx_factory()

    tolerance = 1e-9

    from pytools.convergence import EOCRecorder
    eoc_rec0 = EOCRecorder()
    eoc_rec1 = EOCRecorder()
    # for nel_1d in [4, 8, 12]:
    for nel_1d in [4, 8]:
        from meshmode.mesh.generation import generate_regular_rect_mesh
        mesh = generate_regular_rect_mesh(
            a=(-0.5,) * dim, b=(0.5,) * dim, nelements_per_axis=(nel_1d,) * dim
        )

        logger.info(
            f"Number of {dim}d elements: {mesh.nelements}"
        )

        discr = EagerDGDiscretization(actx, mesh, order=order)
        zeros = discr.zeros(actx)
        ones = zeros + 1.0

        mass_input = discr.zeros(actx) + 1
        energy_input = discr.zeros(actx) + 2.5

        mom_input = make_obj_array(
            [discr.zeros(actx) for i in range(discr.dim)]
        )

        mass_frac_input = flat_obj_array(
            [ones / ((i + 1) * 10) for i in range(nspecies)]
        )
        species_mass_input = mass_input * mass_frac_input
        num_equations = dim + 2 + len(species_mass_input)

        cv = make_conserved(
            dim, mass=mass_input, energy=energy_input, momentum=mom_input,
            species_mass=species_mass_input)

        expected_rhs = split_conserved(
            dim, make_obj_array([discr.zeros(actx)
                                 for i in range(num_equations)])
        )

        boundaries = {BTAG_ALL: DummyBoundary()}
        inviscid_rhs = euler_operator(discr, eos=IdealSingleGas(),
                                      boundaries=boundaries, cv=cv, t=0.0)
        rhs_resid = inviscid_rhs - expected_rhs

        rho_resid = rhs_resid.mass
        rhoe_resid = rhs_resid.energy
        mom_resid = rhs_resid.momentum
        rhoy_resid = rhs_resid.species_mass

        rho_rhs = inviscid_rhs.mass
        rhoe_rhs = inviscid_rhs.energy
        rhov_rhs = inviscid_rhs.momentum
        rhoy_rhs = inviscid_rhs.species_mass

        logger.info(
            f"rho_rhs  = {rho_rhs}\n"
            f"rhoe_rhs = {rhoe_rhs}\n"
            f"rhov_rhs = {rhov_rhs}\n"
            f"rhoy_rhs = {rhoy_rhs}\n"
        )

        assert discr.norm(rho_resid, np.inf) < tolerance
        assert discr.norm(rhoe_resid, np.inf) < tolerance
        for i in range(dim):
            assert discr.norm(mom_resid[i], np.inf) < tolerance
        for i in range(nspecies):
            assert discr.norm(rhoy_resid[i], np.inf) < tolerance

        err_max = discr.norm(rho_resid, np.inf)
        eoc_rec0.add_data_point(1.0 / nel_1d, err_max)

        # set a non-zero, but uniform velocity component
        for i in range(len(mom_input)):
            mom_input[i] = discr.zeros(actx) + (-1.0) ** i

        cv = make_conserved(
            dim, mass=mass_input, energy=energy_input, momentum=mom_input,
            species_mass=species_mass_input)

        boundaries = {BTAG_ALL: DummyBoundary()}
        inviscid_rhs = euler_operator(discr, eos=IdealSingleGas(),
                                      boundaries=boundaries, cv=cv, t=0.0)
        rhs_resid = inviscid_rhs - expected_rhs

        rho_resid = rhs_resid.mass
        rhoe_resid = rhs_resid.energy
        mom_resid = rhs_resid.momentum
        rhoy_resid = rhs_resid.species_mass

        assert discr.norm(rho_resid, np.inf) < tolerance
        assert discr.norm(rhoe_resid, np.inf) < tolerance

        for i in range(dim):
            assert discr.norm(mom_resid[i], np.inf) < tolerance
        for i in range(nspecies):
            assert discr.norm(rhoy_resid[i], np.inf) < tolerance

        err_max = discr.norm(rho_resid, np.inf)
        eoc_rec1.add_data_point(1.0 / nel_1d, err_max)

    logger.info(
        f"V == 0 Errors:\n{eoc_rec0}"
        f"V != 0 Errors:\n{eoc_rec1}"
    )

    assert (
        eoc_rec0.order_estimate() >= order - 0.5
        or eoc_rec0.max_error() < 1e-9
    )
    assert (
        eoc_rec1.order_estimate() >= order - 0.5
        or eoc_rec1.max_error() < 1e-9
    )


@pytest.mark.parametrize("order", [1, 2, 3])
def test_vortex_rhs(actx_factory, order):
    """Tests the inviscid rhs using the non-trivial 2D isentropic vortex
    case configured to yield rhs = 0. Checks several different orders and
    refinement levels to check error behavior.
    """
    actx = actx_factory()

    dim = 2

    from pytools.convergence import EOCRecorder
    eoc_rec = EOCRecorder()

    from meshmode.mesh.generation import generate_regular_rect_mesh

    for nel_1d in [32, 48, 64]:

        mesh = generate_regular_rect_mesh(
            a=(-5,) * dim, b=(5,) * dim, nelements_per_axis=(nel_1d,) * dim,
        )

        logger.info(
            f"Number of {dim}d elements:  {mesh.nelements}"
        )

        discr = EagerDGDiscretization(actx, mesh, order=order)
        nodes = thaw(actx, discr.nodes())

        # Init soln with Vortex and expected RHS = 0
        vortex = Vortex2D(center=[0, 0], velocity=[0, 0])
        vortex_soln = vortex(nodes)
        boundaries = {BTAG_ALL: PrescribedBoundary(vortex)}

        inviscid_rhs = euler_operator(
            discr, eos=IdealSingleGas(), boundaries=boundaries,
            cv=vortex_soln, t=0.0)

        err_max = discr.norm(inviscid_rhs.join(), np.inf)
        eoc_rec.add_data_point(1.0 / nel_1d, err_max)

    logger.info(
        f"Error for (dim,order) = ({dim},{order}):\n"
        f"{eoc_rec}"
    )

    assert (
        eoc_rec.order_estimate() >= order - 0.5
        or eoc_rec.max_error() < 1e-11
    )


@pytest.mark.parametrize("dim", [1, 2, 3])
@pytest.mark.parametrize("order", [1, 2, 3])
def test_lump_rhs(actx_factory, dim, order):
    """Tests the inviscid rhs using the non-trivial 1, 2, and 3D mass lump
    case against the analytic expressions of the RHS. Checks several different
    orders and refinement levels to check error behavior.
    """
    actx = actx_factory()

    tolerance = 1e-10
    maxxerr = 0.0

    from pytools.convergence import EOCRecorder

    eoc_rec = EOCRecorder()

    for nel_1d in [4, 8, 12]:
        from meshmode.mesh.generation import (
            generate_regular_rect_mesh,
        )

        mesh = generate_regular_rect_mesh(
            a=(-5,) * dim, b=(5,) * dim, nelements_per_axis=(nel_1d,) * dim,
        )

        logger.info(f"Number of elements: {mesh.nelements}")

        discr = EagerDGDiscretization(actx, mesh, order=order)
        nodes = thaw(actx, discr.nodes())

        # Init soln with Lump and expected RHS = 0
        center = np.zeros(shape=(dim,))
        velocity = np.zeros(shape=(dim,))
        lump = Lump(dim=dim, center=center, velocity=velocity)
        lump_soln = lump(nodes)
        boundaries = {BTAG_ALL: PrescribedBoundary(lump)}
        inviscid_rhs = euler_operator(
            discr, eos=IdealSingleGas(), boundaries=boundaries, cv=lump_soln, t=0.0)
        expected_rhs = lump.exact_rhs(discr, cv=lump_soln, t=0)

        err_max = discr.norm((inviscid_rhs-expected_rhs).join(), np.inf)
        if err_max > maxxerr:
            maxxerr = err_max

        eoc_rec.add_data_point(1.0 / nel_1d, err_max)
    logger.info(f"Max error: {maxxerr}")

    logger.info(
        f"Error for (dim,order) = ({dim},{order}):\n"
        f"{eoc_rec}"
    )

    assert (
        eoc_rec.order_estimate() >= order - 0.5
        or eoc_rec.max_error() < tolerance
    )


@pytest.mark.parametrize("dim", [1, 2, 3])
@pytest.mark.parametrize("order", [1, 2, 4])
@pytest.mark.parametrize("v0", [0.0, 1.0])
def test_multilump_rhs(actx_factory, dim, order, v0):
    """Tests the inviscid rhs using the non-trivial 1, 2, and 3D mass lump case
    against the analytic expressions of the RHS. Checks several different orders
    and refinement levels to check error behavior.
    """
    actx = actx_factory()
    nspecies = 10
    tolerance = 1e-8
    maxxerr = 0.0

    from pytools.convergence import EOCRecorder

    eoc_rec = EOCRecorder()

    for nel_1d in [4, 8, 16]:
        from meshmode.mesh.generation import (
            generate_regular_rect_mesh,
        )

        mesh = generate_regular_rect_mesh(
            a=(-1,) * dim, b=(1,) * dim, nelements_per_axis=(nel_1d,) * dim,
        )

        logger.info(f"Number of elements: {mesh.nelements}")

        discr = EagerDGDiscretization(actx, mesh, order=order)
        nodes = thaw(actx, discr.nodes())

        centers = make_obj_array([np.zeros(shape=(dim,)) for i in range(nspecies)])
        spec_y0s = np.ones(shape=(nspecies,))
        spec_amplitudes = np.ones(shape=(nspecies,))

        velocity = np.zeros(shape=(dim,))
        velocity[0] = v0
        rho0 = 2.0

        lump = MulticomponentLump(dim=dim, nspecies=nspecies, rho0=rho0,
                                  spec_centers=centers, velocity=velocity,
                                  spec_y0s=spec_y0s, spec_amplitudes=spec_amplitudes)

        lump_soln = lump(nodes)
        boundaries = {BTAG_ALL: PrescribedBoundary(lump)}

        inviscid_rhs = euler_operator(
            discr, eos=IdealSingleGas(), boundaries=boundaries, cv=lump_soln, t=0.0)
        expected_rhs = lump.exact_rhs(discr, cv=lump_soln, t=0)
        print(f"inviscid_rhs = {inviscid_rhs}")
        print(f"expected_rhs = {expected_rhs}")
        err_max = discr.norm((inviscid_rhs-expected_rhs).join(), np.inf)
        if err_max > maxxerr:
            maxxerr = err_max

        eoc_rec.add_data_point(1.0 / nel_1d, err_max)
    logger.info(f"Max error: {maxxerr}")

    logger.info(
        f"Error for (dim,order) = ({dim},{order}):\n"
        f"{eoc_rec}"
    )

    assert (
        eoc_rec.order_estimate() >= order - 0.5
        or eoc_rec.max_error() < tolerance
    )


def _euler_flow_stepper(actx, parameters):
    """
    Implements a generic time stepping loop for testing an inviscid flow
    using a spectral filter.
    """
    logging.basicConfig(format="%(message)s", level=logging.INFO)

    mesh = parameters["mesh"]
    t = parameters["time"]
    order = parameters["order"]
    t_final = parameters["tfinal"]
    initializer = parameters["initializer"]
    exittol = parameters["exittol"]
    casename = parameters["casename"]
    boundaries = parameters["boundaries"]
    eos = parameters["eos"]
    cfl = parameters["cfl"]
    dt = parameters["dt"]
    constantcfl = parameters["constantcfl"]
    nstepstatus = parameters["nstatus"]

    if t_final <= t:
        return(0.0)

    rank = 0
    dim = mesh.dim
    istep = 0

    discr = EagerDGDiscretization(actx, mesh, order=order)
    nodes = thaw(actx, discr.nodes())

    cv = initializer(nodes)
    sdt = cfl * get_inviscid_timestep(discr, eos=eos, cv=cv)

    initname = initializer.__class__.__name__
    eosname = eos.__class__.__name__
    logger.info(
        f"Num {dim}d order-{order} elements: {mesh.nelements}\n"
        f"Timestep:        {dt}\n"
        f"Final time:      {t_final}\n"
        f"Status freq:     {nstepstatus}\n"
        f"Initialization:  {initname}\n"
        f"EOS:             {eosname}"
    )

    vis = make_visualizer(discr, order)

    def write_soln(state, write_status=True):
        dv = eos.dependent_vars(cv=state)
        expected_result = initializer(nodes, t=t)
        result_resid = (state - expected_result).join()
        maxerr = [np.max(np.abs(result_resid[i].get())) for i in range(dim + 2)]
        mindv = [np.min(dvfld.get()) for dvfld in dv]
        maxdv = [np.max(dvfld.get()) for dvfld in dv]

        if write_status is True:
            statusmsg = (
                f"Status: Step({istep}) Time({t})\n"
                f"------   P({mindv[0]},{maxdv[0]})\n"
                f"------   T({mindv[1]},{maxdv[1]})\n"
                f"------   dt,cfl = ({dt},{cfl})\n"
                f"------   Err({maxerr})"
            )
            logger.info(statusmsg)

        io_fields = ["cv", state]
        io_fields += eos.split_fields(dim, dv)
        io_fields.append(("exact_soln", expected_result))
        io_fields.append(("residual", result_resid))
        nameform = casename + "-{iorank:04d}-{iostep:06d}.vtu"
        visfilename = nameform.format(iorank=rank, iostep=istep)
        vis.write_vtk_file(visfilename, io_fields)

        return maxerr

    def rhs(t, q):
        return euler_operator(discr, eos=eos, boundaries=boundaries, cv=q, t=t)

    filter_order = 8
    eta = .5
    alpha = -1.0*np.log(np.finfo(float).eps)
    nummodes = int(1)
    for _ in range(dim):
        nummodes *= int(order + dim + 1)
    nummodes /= math.factorial(int(dim))
    cutoff = int(eta * order)

    from mirgecom.filter import (
        exponential_mode_response_function as xmrfunc,
        filter_modally
    )
    frfunc = partial(xmrfunc, alpha=alpha, filter_order=filter_order)

    while t < t_final:

        if constantcfl is True:
            dt = sdt
        else:
            cfl = dt / sdt

        if nstepstatus > 0:
            if istep % nstepstatus == 0:
                write_soln(state=cv)

        cv = rk4_step(cv, t, dt, rhs)
        cv = split_conserved(
            dim, filter_modally(discr, "vol", cutoff, frfunc, cv.join())
        )

        t += dt
        istep += 1

        sdt = cfl * get_inviscid_timestep(discr, eos=eos, cv=cv)

    if nstepstatus > 0:
        logger.info("Writing final dump.")
        maxerr = max(write_soln(cv, False))
    else:
        expected_result = initializer(nodes, t=t)
        maxerr = discr.norm((cv - expected_result).join(), np.inf)

    logger.info(f"Max Error: {maxerr}")
    if maxerr > exittol:
        raise ValueError("Solution failed to follow expected result.")

    return(maxerr)


@pytest.mark.parametrize("order", [2, 3, 4])
def test_isentropic_vortex(actx_factory, order):
    """Advance the 2D isentropic vortex case in time with non-zero velocities
    using an RK4 timestepping scheme. Check the advanced field values against
    the exact/analytic expressions.

    This tests all parts of the Euler module working together, with results
    converging at the expected rates vs. the order.
    """
    actx = actx_factory()

    dim = 2

    from pytools.convergence import EOCRecorder

    eoc_rec = EOCRecorder()

    for nel_1d in [16, 32, 64]:
        from meshmode.mesh.generation import (
            generate_regular_rect_mesh,
        )

        mesh = generate_regular_rect_mesh(
            a=(-5.0,) * dim, b=(5.0,) * dim, nelements_per_axis=(nel_1d,) * dim
        )

        exittol = 1.0
        t_final = 0.001
        cfl = 1.0
        vel = np.zeros(shape=(dim,))
        orig = np.zeros(shape=(dim,))
        vel[:dim] = 1.0
        dt = .0001
        initializer = Vortex2D(center=orig, velocity=vel)
        casename = "Vortex"
        boundaries = {BTAG_ALL: PrescribedBoundary(initializer)}
        eos = IdealSingleGas()
        t = 0
        flowparams = {"dim": dim, "dt": dt, "order": order, "time": t,
                      "boundaries": boundaries, "initializer": initializer,
                      "eos": eos, "casename": casename, "mesh": mesh,
                      "tfinal": t_final, "exittol": exittol, "cfl": cfl,
                      "constantcfl": False, "nstatus": 0}
        maxerr = _euler_flow_stepper(actx, flowparams)
        eoc_rec.add_data_point(1.0 / nel_1d, maxerr)

    logger.info(
        f"Error for (dim,order) = ({dim},{order}):\n"
        f"{eoc_rec}"
    )

    assert (
        eoc_rec.order_estimate() >= order - 0.5
        or eoc_rec.max_error() < 1e-11
    )
