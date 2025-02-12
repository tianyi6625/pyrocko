# http://pyrocko.org - GPLv3
#
# The Pyrocko Developers, 21st Century
# ---|P------/S----------~Lg----------

import logging
import warnings
import os
from os.path import join as pjoin
import signal
import shutil
import copy

import math
import numpy as num

from tempfile import mkdtemp
from subprocess import Popen, PIPE

from pyrocko.guts import Float, Int, Tuple, List, Object, String
from pyrocko.model import Location
from pyrocko import gf, util, trace, cake


# how to call the programs
program_bins = {
    'pscmp.2008a': 'fomosto_pscmp2008a',
    'psgrn.2008a': 'fomosto_psgrn2008a'
}

psgrn_displ_names = ('uz', 'ur', 'ut')
psgrn_stress_names = ('szz', 'srr', 'stt', 'szr', 'srt', 'stz')
psgrn_tilt_names = ('tr', 'tt', 'rot')
psgrn_gravity_names = ('gd', 'gr')
psgrn_components = 'ep ss ds cl'.split()

km = 1000.
day = 3600. * 24

guts_prefix = 'pf'
logger = logging.getLogger('pyrocko.fomosto.psgrn_pscmp')


def have_backend():
    for cmd in [[exe] for exe in program_bins.values()]:
        try:
            p = Popen(cmd, stdout=PIPE, stderr=PIPE, stdin=PIPE)
            (stdout, stderr) = p.communicate()

        except OSError:
            return False

    return True


def nextpow2(i):
    return 2 ** int(math.ceil(math.log(i) / math.log(2.)))


def str_float_vals(vals):
    return ' '.join('%e' % val for val in vals)


def str_int_vals(vals):
    return ' '.join('%i' % val for val in vals)


def str_str_vals(vals):
    return ' '.join("'%s'" % val for val in vals)


def cake_model_to_config(mod):
    srows = []
    for ir, row in enumerate(mod.to_scanlines(get_burgers=True)):
        depth, vp, vs, rho, qp, qs, eta1, eta2, alpha = row
        # replace qs with etas = 0.
        row = [depth / km, vp / km, vs / km, rho, eta1, eta2, alpha]
        srows.append('%i %15s' % (ir + 1, str_float_vals(row)))

    return '\n'.join(srows), len(srows)


class PsGrnSpatialSampling(Object):

    '''
    Definition of spatial sampling for PSGRN.

    Note: attributes in this class use non-standard units [km] to be consistent
    with PSGRN text file input. Please read the documentation carefully.
    '''

    n_steps = Int.T(default=10)
    start_distance = Float.T(default=0.)    # start sampling [km] from source
    end_distance = Float.T(default=100.)    # end

    def string_for_config(self):
        return '%i %15e %15e' % (self.n_steps, self.start_distance,
                                 self.end_distance)


class PsGrnConfig(Object):

    '''
    Configuration for PSGRN.

    Note: attributes in this class use non-standard units [km] and [days] to
    be consistent with PSGRN text file input. Please read the documentation
    carefully.
    '''

    version = String.T(default='2008a')

    sampling_interval = Float.T(
        default=1.0,
        help='Exponential sampling 1.- equidistant, > 1. decreasing sampling'
             ' with distance')
    n_time_samples = Int.T(
        optional=True,
        help='Number of temporal GF samples up to max_time. Has to be equal'
             ' to a power of 2! If not, next power of 2 is taken.')
    max_time = Float.T(
        optional=True,
        help='Maximum time [days] after seismic event.')

    gf_depth_spacing = Float.T(
        optional=True,
        help='Depth spacing [km] for the observation points. '
             'If not defined depth spacing from the `StoreConfig` is used')
    gf_distance_spacing = Float.T(
        optional=True,
        help='Spatial spacing [km] for the observation points. '
             'If not defined distance spacing from the `StoreConfig` is used')
    observation_depth = Float.T(
        default=0.,
        help='Depth of observation points [km]')

    def items(self):
        return dict(self.T.inamevals(self))


class PsGrnConfigFull(PsGrnConfig):

    earthmodel_1d = gf.meta.Earthmodel1D.T(optional=True)
    psgrn_outdir = String.T(default='psgrn_green/')

    distance_grid = PsGrnSpatialSampling.T(PsGrnSpatialSampling.D())
    depth_grid = PsGrnSpatialSampling.T(PsGrnSpatialSampling.D())

    sw_source_regime = Int.T(default=1)         # 1-continental, 0-ocean
    sw_gravity = Int.T(default=0)

    accuracy_wavenumber_integration = Float.T(default=0.025)

    displ_filenames = Tuple.T(3, String.T(), default=psgrn_displ_names)
    stress_filenames = Tuple.T(6, String.T(), default=psgrn_stress_names)
    tilt_filenames = Tuple.T(3, String.T(), psgrn_tilt_names)
    gravity_filenames = Tuple.T(2, String.T(), psgrn_gravity_names)

    @staticmethod
    def example():
        conf = PsGrnConfigFull()
        conf.earthmodel_1d = cake.load_model().extract(depth_max=100*km)
        conf.psgrn_outdir = 'TEST_psgrn_functions/'
        return conf

    def string_for_config(self):

        assert self.earthmodel_1d is not None

        d = self.__dict__.copy()

        model_str, nlines = cake_model_to_config(self.earthmodel_1d)
        d['n_t2'] = nextpow2(self.n_time_samples)
        d['n_model_lines'] = nlines
        d['model_lines'] = model_str

        d['str_psgrn_outdir'] = "'%s'" % './'

        d['str_displ_filenames'] = str_str_vals(self.displ_filenames)
        d['str_stress_filenames'] = str_str_vals(self.stress_filenames)
        d['str_tilt_filenames'] = str_str_vals(self.tilt_filenames)
        d['str_gravity_filenames'] = str_str_vals(self.gravity_filenames)

        d['str_distance_grid'] = self.distance_grid.string_for_config()
        d['str_depth_grid'] = self.depth_grid.string_for_config()

        template = '''# autogenerated PSGRN input by psgrn.py
#=============================================================================
# This is input file of FORTRAN77 program "psgrn08a" for computing responses
# (Green's functions) of a multi-layered viscoelastic halfspace to point
# dislocation sources buried at different depths. All results will be stored in
# the given directory and provide the necessary data base for the program
# "pscmp07a" for computing time-dependent deformation, geoid and gravity changes
# induced by an earthquake with extended fault planes via linear superposition.
# For more details, please read the accompanying READ.ME file.
#
# written by Rongjiang Wang
# GeoForschungsZentrum Potsdam
# e-mail: wang@gfz-potsdam.de
# phone +49 331 2881209
# fax +49 331 2881204
#
# Last modified: Potsdam, Jan, 2008
#
#################################################################
##                                                             ##
## Cylindrical coordinates (Z positive downwards!) are used.   ##
##                                                             ##
## If not specified otherwise, SI Unit System is used overall! ##
##                                                             ##
#################################################################
#
#------------------------------------------------------------------------------
#
#        PARAMETERS FOR SOURCE-OBSERVATION CONFIGURATIONS
#        ================================================
# 1. the uniform depth of the observation points [km], switch for oceanic (0)
#    or continental(1) earthquakes;
# 2. number of (horizontal) observation distances (> 1 and <= nrmax defined in
#    psgglob.h), start and end distances [km], ratio (>= 1.0) between max. and
#    min. sampling interval (1.0 for equidistant sampling);
# 3. number of equidistant source depths (>= 1 and <= nzsmax defined in
#    psgglob.h), start and end source depths [km];
#
#    r1,r2 = minimum and maximum horizontal source-observation
#            distances (r2 > r1).
#    zs1,zs2 = minimum and maximum source depths (zs2 >= zs1 > 0).
#
#    Note that the same sampling rates dr_min and dzs will be used later by the
#    program "pscmp07a" for discretizing the finite source planes to a 2D grid
#    of point sources.
#------------------------------------------------------------------------------
 %(observation_depth)e  %(sw_source_regime)i
 %(str_distance_grid)s  %(sampling_interval)e
 %(str_depth_grid)s
#------------------------------------------------------------------------------
#
#        PARAMETERS FOR TIME SAMPLING
#        ============================
# 1. number of time samples (<= ntmax def. in psgglob.h) and time window [days].
#
#    Note that nt (> 0) should be power of 2 (the fft-rule). If nt = 1, the
#    coseismic (t = 0) changes will be computed; If nt = 2, the coseismic
#    (t = 0) and steady-state (t -> infinity) changes will be computed;
#    Otherwise, time series for the given time samples will be computed.
#
#------------------------------------------------------------------------------
 %(n_t2)i    %(max_time)f
#------------------------------------------------------------------------------
#
#        PARAMETERS FOR WAVENUMBER INTEGRATION
#        =====================================
# 1. relative accuracy of the wave-number integration (suggested: 0.1 - 0.01)
# 2. factor (> 0 and < 1) for including influence of earth's gravity on the
#    deformation field (e.g. 0/1 = without / with 100percent gravity effect).
#------------------------------------------------------------------------------
 %(accuracy_wavenumber_integration)e
 %(sw_gravity)i
#------------------------------------------------------------------------------
#
#        PARAMETERS FOR OUTPUT FILES
#        ===========================
#
# 1. output directory
# 2. file names for 3 displacement components (uz, ur, ut)
# 3. file names for 6 stress components (szz, srr, stt, szr, srt, stz)
# 4. file names for radial and tangential tilt components (as measured by a
#    borehole tiltmeter), rigid rotation of horizontal plane, geoid and gravity
#    changes (tr, tt, rot, gd, gr)
#
#    Note that all file or directory names should not be longer than 80
#    characters. Directory and subdirectoy names must be separated and ended
#    by / (unix) or \ (dos)! All file names should be given without extensions
#    that will be appended automatically by ".ep" for the explosion (inflation)
#    source, ".ss" for the strike-slip source, ".ds" for the dip-slip source,
#    and ".cl" for the compensated linear vector dipole source)
#
#------------------------------------------------------------------------------
 %(str_psgrn_outdir)s
 %(str_displ_filenames)s
 %(str_stress_filenames)s
 %(str_tilt_filenames)s %(str_gravity_filenames)s
#------------------------------------------------------------------------------
#
#        GLOBAL MODEL PARAMETERS
#        =======================
# 1. number of data lines of the layered model (<= lmax as defined in psgglob.h)
#
#    The surface and the upper boundary of the half-space as well as the
#    interfaces at which the viscoelastic parameters are continuous, are all
#    defined by a single data line; All other interfaces, at which the
#    viscoelastic parameters are discontinuous, are all defined by two data
#    lines (upper-side and lower-side values). This input format could also be
#    used for a graphic plot of the layered model. Layers which have different
#    parameter values at top and bottom, will be treated as layers with a
#    constant gradient, and will be discretised to a number of homogeneous
#    sublayers. Errors due to the discretisation are limited within about
#    5percent (changeable, see psgglob.h).
#
# 2....        parameters of the multilayered model
#
#    Burgers rheology (a Kelvin-Voigt body and a Maxwell body in series
#    connection) for relaxation of shear modulus is implemented. No relaxation
#    of compressional modulus is considered.
#
#    eta1  = transient viscosity (dashpot of the Kelvin-Voigt body; <= 0 means
#            infinity value)
#    eta2  = steady-state viscosity (dashpot of the Maxwell body; <= 0 means
#            infinity value)
#    alpha = ratio between the effective and the unrelaxed shear modulus
#            = mu1/(mu1+mu2) (> 0 and <= 1)
#
#    Special cases:
#        (1) Elastic: eta1 and eta2 <= 0 (i.e. infinity); alpha meaningless
#        (2) Maxwell body: eta1 <= 0 (i.e. eta1 = infinity)
#                          or alpha = 1 (i.e. mu1 = infinity)
#        (3) Standard-Linear-Solid: eta2 <= 0 (i.e. infinity)
#------------------------------------------------------------------------------
 %(n_model_lines)i                               |int: no_model_lines;
#------------------------------------------------------------------------------
# no  depth[km]  vp[km/s]  vs[km/s]  rho[kg/m^3] eta1[Pa*s] eta2[Pa*s] alpha
#------------------------------------------------------------------------------
%(model_lines)s
#=======================end of input===========================================
'''  # noqa
        return (template % d).encode('ascii')


class PsGrnError(gf.store.StoreError):
    pass


def remove_if_exists(fn, force=False):
    if os.path.exists(fn):
        if force:
            os.remove(fn)
        else:
            raise gf.CannotCreate('file %s already exists' % fn)


class PsGrnRunner(object):
    '''
    Wrapper object to execute the program fomosto_psgrn.
    '''

    def __init__(self, outdir):
        outdir = os.path.abspath(outdir)
        if not os.path.exists(outdir):
            os.mkdir(outdir)
        self.outdir = outdir
        self.config = None

    def run(self, config, force=False):
        '''
        Run the program with the specified configuration.

        :param config: :py:class:`PsGrnConfigFull`
        :param force: boolean, set true to overwrite existing output
        '''
        self.config = config

        input_fn = pjoin(self.outdir, 'input')

        remove_if_exists(input_fn, force=force)

        with open(input_fn, 'wb') as f:
            input_str = config.string_for_config()
            logger.debug('===== begin psgrn input =====\n'
                         '%s===== end psgrn input =====' % input_str.decode())
            f.write(input_str)
        program = program_bins['psgrn.%s' % config.version]

        old_wd = os.getcwd()

        os.chdir(self.outdir)

        interrupted = []

        def signal_handler(signum, frame):
            os.kill(proc.pid, signal.SIGTERM)
            interrupted.append(True)

        original = signal.signal(signal.SIGINT, signal_handler)
        try:
            try:
                proc = Popen(program, stdin=PIPE, stdout=PIPE, stderr=PIPE)

            except OSError:
                os.chdir(old_wd)
                raise PsGrnError(
                    '''could not start psgrn executable: "%s"
Available fomosto backends and download links to the modelling codes are listed
on

      https://pyrocko.org/docs/current/apps/fomosto/backends.html

''' % program)

            (output_str, error_str) = proc.communicate(b'input\n')

        finally:
            signal.signal(signal.SIGINT, original)

        if interrupted:
            raise KeyboardInterrupt()

        logger.debug('===== begin psgrn output =====\n'
                     '%s===== end psgrn output =====' % output_str.decode())

        errmess = []
        if proc.returncode != 0:
            errmess.append(
                'psgrn had a non-zero exit state: %i' % proc.returncode)

        if error_str:
            logger.warning(
                'psgrn emitted something via stderr: \n\n%s'
                % error_str.decode())
            # errmess.append('psgrn emitted something via stderr')

        if output_str.lower().find(b'error') != -1:
            errmess.append("the string 'error' appeared in psgrn output")

        if errmess:
            os.chdir(old_wd)
            raise PsGrnError('''
===== begin psgrn input =====
%s===== end psgrn input =====
===== begin psgrn output =====
%s===== end psgrn output =====
===== begin psgrn error =====
%s===== end psgrn error =====
%s
psgrn has been invoked as "%s"
in the directory %s'''.lstrip() % (
                input_str.decode(), output_str.decode(), error_str.decode(),
                '\n'.join(errmess), program, self.outdir))

        self.psgrn_output = output_str
        self.psgrn_error = error_str

        os.chdir(old_wd)


pscmp_displ_names = ('un', 'ue', 'ud')
pscmp_stress_names = ('snn', 'see', 'sdd', 'sne', 'snd', 'sed')
pscmp_tilt_names = ('tn', 'te', 'rot')
pscmp_gravity_names = ('gd', 'gr')
pscmp_all_names = pscmp_displ_names + pscmp_stress_names + pscmp_tilt_names + \
    pscmp_gravity_names

pscmp_component_mapping = {
    'displ': (pscmp_displ_names, (2, 5)),
    'stress': (pscmp_stress_names, (5, 11)),
    'tilt': (pscmp_tilt_names, (11, 14)),
    'gravity': (pscmp_gravity_names, (14, 16)),
    'all': (pscmp_all_names, (2, 16)),
                            }


def dsin(value):
    return num.sin(value * num.pi / 180.)


def dcos(value):
    return num.cos(value * num.pi / 180.)


def distributed_fault_patches_to_config(patches):
    '''
    Input: List of PsCmpRectangularSource(s)
    '''
    srows = []
    for i, patch in enumerate(patches):
        srows.append('%i %s' % (i + 1, patch.string_for_config()))

    return '\n'.join(srows), len(srows)


class PsCmpObservation(Object):
    pass


class PsCmpScatter(PsCmpObservation):
    '''
    Scattered observation points.
    '''
    lats = List.T(Float.T(), optional=True, default=[10.4, 10.5])
    lons = List.T(Float.T(), optional=True, default=[12.3, 13.4])

    def string_for_config(self):
        srows = []
        for lat, lon in zip(self.lats, self.lons):
            srows.append('(%15f, %15f)' % (lat, lon))

        self.sw = 0
        return ' %i' % (len(srows)), '\n'.join(srows)


class PsCmpProfile(PsCmpObservation):
    '''
    Calculation along observation profile.
    '''
    n_steps = Int.T(default=10)
    slat = Float.T(
        default=0.,
        help='Profile start latitude')
    slon = Float.T(
        default=0.,
        help='Profile start longitude')
    elat = Float.T(
        default=0.,
        help='Profile end latitude')
    elon = Float.T(
        default=0.,
        help='Profile end longitude')
    distances = List.T(
        Float.T(),
        optional=True,
        help='Distances [m] for each point on profile from start to end.')

    def string_for_config(self):
        self.sw = 1

        return ' %i' % (self.n_steps), \
            ' ( %15f, %15f ), ( %15f, %15f )' % (
                                self.slat, self.slon, self.elat, self.elon)


class PsCmpArray(PsCmpObservation):
    '''
    Calculation on a grid.
    '''
    n_steps_lat = Int.T(default=10)
    n_steps_lon = Int.T(default=10)
    slat = Float.T(
        default=0.,
        help='Array start latitude')
    slon = Float.T(
        default=0.,
        help='Array start longitude')
    elat = Float.T(
        default=0.,
        help='Array end latitude')
    elon = Float.T(
        default=0.,
        help='Array end longitude')

    def string_for_config(self):
        self.sw = 2

        return ' %i %15f %15f ' % (
                self.n_steps_lat, self.slat, self.elat), \
               ' %i %15f %15f ' % (
                self.n_steps_lon, self.slon, self.elon)


class PsCmpRectangularSource(Location, gf.seismosizer.Cloneable):
    '''
    Rectangular Source for the input geometry of the active fault.

    Input parameters have to be in:
    [deg] for reference point (lat, lon) and angles (rake, strike, dip)
    [m] shifting with respect to reference position
    [m] for fault dimensions and source depth. The default shift of the
    origin (:py:attr`pos_s`, :py:attr:`pos_d`) with respect to the reference
        coordinates
    (lat, lon) is zero, which implies that the reference is the center of
        the fault plane!
    The calculation point is always the center of the fault-plane!
    Setting :py:attr`pos_s` or :py:attr`pos_d` moves the fault point with
        respect to the origin along strike and dip direction, respectively!
    '''
    length = Float.T(default=6.0 * km)
    width = Float.T(default=5.0 * km)
    strike = Float.T(default=0.0)
    dip = Float.T(default=90.0)
    rake = Float.T(default=0.0)
    torigin = Float.T(default=0.0)

    slip = Float.T(optional=True, default=1.0)

    pos_s = Float.T(optional=True, default=None)
    pos_d = Float.T(optional=True, default=None)
    opening = Float.T(default=0.0)

    def update(self, **kwargs):
        '''
        Change some of the source models parameters.

        Example::

          >>> from pyrocko import gf
          >>> s = gf.DCSource()
          >>> s.update(strike=66., dip=33.)
          >>> print(s)
          --- !pf.DCSource
          depth: 0.0
          time: 1970-01-01 00:00:00
          magnitude: 6.0
          strike: 66.0
          dip: 33.0
          rake: 0.0

        '''
        for (k, v) in kwargs.items():
            self[k] = v

    @property
    def dip_slip(self):
        return float(self.slip * dsin(self.rake) * (-1))

    @property
    def strike_slip(self):
        return float(self.slip * dcos(self.rake))

    def string_for_config(self):

        if self.pos_s or self.pos_d is None:
            self.pos_s = 0.
            self.pos_d = 0.

        tempd = copy.deepcopy(self.__dict__)
        tempd['dip_slip'] = self.dip_slip
        tempd['strike_slip'] = self.strike_slip
        tempd['effective_lat'] = self.effective_lat
        tempd['effective_lon'] = self.effective_lon
        tempd['depth'] /= km
        tempd['length'] /= km
        tempd['width'] /= km

        return '%(effective_lat)15f %(effective_lon)15f %(depth)15f' \
               '%(length)15f %(width)15f %(strike)15f' \
               '%(dip)15f 1 1 %(torigin)15f \n %(pos_s)15f %(pos_d)15f ' \
               '%(strike_slip)15f %(dip_slip)15f %(opening)15f' % tempd


MTIso = {
    'nn': dict(strike=90., dip=90., rake=0., slip=0., opening=1.),
    'ee': dict(strike=0., dip=90., rake=0., slip=0., opening=1.),
    'dd': dict(strike=0., dip=0., rake=-90., slip=0., opening=1.),
    }

MTDev = {
    'ne': dict(strike=90., dip=90., rake=180., slip=1., opening=0.),
    'nd': dict(strike=180., dip=0., rake=0., slip=1., opening=0.),
    'ed': dict(strike=270., dip=0., rake=0., slip=1., opening=0.),
    }


def get_nullification_factor(mu, lame_lambda):
    '''
    Factor that needs to be multiplied to 2 of the tensile sources to
    eliminate two of the isotropic components.
    '''
    return -lame_lambda / (2. * mu + 2. * lame_lambda)


def get_trace_normalization_factor(mu, lame_lambda, nullification):
    '''
    Factor to be multiplied to elementary GF trace to have unit displacement.
    '''
    return 1. / ((2. * mu) + lame_lambda + (2. * lame_lambda * nullification))


def get_iso_scaling_factors(mu, lame_lambda):
    nullf = get_nullification_factor(mu, lame_lambda)
    scale = get_trace_normalization_factor(mu, lame_lambda, nullf)
    return nullf, scale


class PsCmpTensileSF(Location, gf.seismosizer.Cloneable):
    '''
    Compound dislocation of 3 perpendicular, rectangular sources to approximate
    an opening single force couple. NED coordinate system!

    Warning: Mixed standard [m] / non-standard [days] units are used in this
    class. Please read the documentation carefully.
    '''

    length = Float.T(
        default=1.0 * km,
        help='Length of source rectangle [m].')
    width = Float.T(
        default=1.0 * km,
        help='Width of source rectangle [m].')
    torigin = Float.T(
        default=0.0,
        help='Origin time [days]')
    idx = String.T(
        default='nn',
        help='Axis index for opening direction; "nn", "ee", or "dd"')

    def to_rfs(self, nullification):

        cmpd = []
        for comp, mt in MTIso.items():
            params = copy.deepcopy(mt)

            if comp != self.idx:
                params = copy.deepcopy(mt)
                params['opening'] *= nullification

            kwargs = copy.deepcopy(self.__dict__)
            kwargs.update(params)
            kwargs.pop('idx')
            kwargs.pop('_latlon')
            cmpd.append(PsCmpRectangularSource(**kwargs))

        return cmpd


class PsCmpShearSF(Location, gf.seismosizer.Cloneable):
    '''
    Shear fault source model component.

    Warning: Mixed standard [m] / non-standard [days] units are used in this
    class. Please read the documentation carefully.
    '''

    length = Float.T(
        default=1.0 * km,
        help='Length of source rectangle [m].')
    width = Float.T(
        default=1.0 * km,
        help='Width of source rectangle [m].')
    torigin = Float.T(
        default=0.0,
        help='Origin time [days]')
    idx = String.T(
        default='ne',
        help='Axis index for opening direction; "ne", "nd", or "ed"')

    def to_rfs(self):
        kwargs = copy.deepcopy(self.__dict__)
        kwargs.pop('idx')
        kwargs.pop('_latlon')
        kwargs.update(MTDev[self.idx])
        return [PsCmpRectangularSource(**kwargs)]


class PsCmpMomentTensor(Location, gf.seismosizer.Cloneable):
    '''
    Mapping of Moment Tensor components to rectangular faults.
    Only one component at a time valid! NED coordinate system!

    Warning: Mixed standard [m] / non-standard [days] units are used in this
    class. Please read the documentation carefully.
    '''
    length = Float.T(
        default=1.0 * km,
        help='Length of source rectangle [m].')
    width = Float.T(
        default=1.0 * km,
        help='Width of source rectangle [m].')
    torigin = Float.T(
        default=0.0,
        help='Origin time [days]')
    idx = String.T(
        default='nn',
        help='Axis index for MT component; '
             '"nn", "ee", "dd", "ne", "nd", or "ed".')

    def to_rfs(self, nullification=-0.25):
        kwargs = copy.deepcopy(self.__dict__)
        kwargs.pop('_latlon')

        if self.idx in MTIso:
            tsf = PsCmpTensileSF(**kwargs)
            return tsf.to_rfs(nullification)

        elif self.idx in MTDev:
            ssf = PsCmpShearSF(**kwargs)
            return ssf.to_rfs()

        else:
            raise Exception('MT component not supported!')


class PsCmpCoulombStress(Object):
    pass


class PsCmpCoulombStressMasterFault(PsCmpCoulombStress):
    friction = Float.T(default=0.7)
    skempton_ratio = Float.T(default=0.0)
    master_fault_strike = Float.T(default=300.)
    master_fault_dip = Float.T(default=15.)
    master_fault_rake = Float.T(default=90.)
    sigma1 = Float.T(default=1.e6, help='[Pa]')
    sigma2 = Float.T(default=-1.e6, help='[Pa]')
    sigma3 = Float.T(default=0.0, help='[Pa]')

    def string_for_config(self):
        return '%(friction)15e %(skempton_ratio)15e %(master_fault_strike)15f'\
               '%(master_fault_dip)15f %(master_fault_rake)15f'\
               '%(sigma1)15e %(sigma2)15e %(sigma3)15e' % self.__dict__


class PsCmpSnapshots(Object):
    '''
    Snapshot time series definition.

    Note: attributes in this class use non-standard units [days] to be
    consistent with PSCMP text file input. Please read the documentation
    carefully.
    '''

    tmin = Float.T(
        default=0.0,
        help='Time [days] after source time to start temporal sample'
             ' snapshots.')
    tmax = Float.T(
        default=1.0,
        help='Time [days] after source time to end temporal sample f.')
    deltatdays = Float.T(
        default=1.0,
        help='Sample period [days].')

    @property
    def times(self):
        nt = int(num.ceil((self.tmax - self.tmin) / self.deltatdays))
        return num.linspace(self.tmin, self.tmax, nt).tolist()

    @property
    def deltat(self):
        return self.deltatdays * 24 * 3600


class PsCmpConfig(Object):

    version = String.T(default='2008a')
    # scatter, profile or array
    observation = PsCmpObservation.T(default=PsCmpScatter.D())

    rectangular_fault_size_factor = Float.T(
        default=1.,
        help='The size of the rectangular faults in the compound MT'
             ' :py:class:`PsCmpMomentTensor` is dependend on the horizontal'
             ' spacing of the GF database. This factor is applied to the'
             ' relationship in i.e. fault length & width = f * dx.')

    snapshots = PsCmpSnapshots.T(
        optional=True)

    rectangular_source_patches = List.T(PsCmpRectangularSource.T())

    def items(self):
        return dict(self.T.inamevals(self))


class PsCmpConfigFull(PsCmpConfig):

    pscmp_outdir = String.T(default='./')
    psgrn_outdir = String.T(default='./psgrn_functions/')

    los_vector = Tuple.T(3, Float.T(), optional=True)

    sw_los_displacement = Int.T(default=0)
    sw_coulomb_stress = Int.T(default=0)
    coulomb_master_field = PsCmpCoulombStress.T(
        optional=True,
        default=PsCmpCoulombStressMasterFault.D())

    displ_sw_output_types = Tuple.T(3, Int.T(), default=(0, 0, 0))
    stress_sw_output_types = Tuple.T(6, Int.T(), default=(0, 0, 0, 0, 0, 0))
    tilt_sw_output_types = Tuple.T(3, Int.T(), default=(0, 0, 0))
    gravity_sw_output_types = Tuple.T(2, Int.T(), default=(0, 0))

    indispl_filenames = Tuple.T(3, String.T(), default=psgrn_displ_names)
    instress_filenames = Tuple.T(6, String.T(), default=psgrn_stress_names)
    intilt_filenames = Tuple.T(3, String.T(), default=psgrn_tilt_names)
    ingravity_filenames = Tuple.T(2, String.T(), default=psgrn_gravity_names)

    outdispl_filenames = Tuple.T(3, String.T(), default=pscmp_displ_names)
    outstress_filenames = Tuple.T(6, String.T(), default=pscmp_stress_names)
    outtilt_filenames = Tuple.T(3, String.T(), default=pscmp_tilt_names)
    outgravity_filenames = Tuple.T(2, String.T(), default=pscmp_gravity_names)

    snapshot_basefilename = String.T(default='snapshot')

    @classmethod
    def example(cls):
        conf = cls()
        conf.psgrn_outdir = 'TEST_psgrn_functions/'
        conf.pscmp_outdir = 'TEST_pscmp_output/'
        conf.rectangular_source_patches = [PsCmpRectangularSource(
                                lat=10., lon=10., slip=2.,
                                width=5., length=10.,
                                strike=45, dip=30, rake=-90)]
        conf.observation = PsCmpArray(
                slat=9.5, elat=10.5, n_steps_lat=150,
                slon=9.5, elon=10.5, n_steps_lon=150)
        return conf

    def get_output_filenames(self, rundir):
        return [pjoin(rundir,
                      self.snapshot_basefilename + '_' + str(i + 1) + '.txt')
                for i in range(len(self.snapshots.times))]

    def string_for_config(self):
        d = self.__dict__.copy()

        patches_str, n_patches = distributed_fault_patches_to_config(
                        self.rectangular_source_patches)

        d['patches_str'] = patches_str
        d['n_patches'] = n_patches

        str_npoints, str_observation = self.observation.string_for_config()
        d['str_npoints'] = str_npoints
        d['str_observation'] = str_observation
        d['sw_observation_type'] = self.observation.sw

        if self.snapshots.times:
            str_times_dummy = []
            for i, time in enumerate(self.snapshots.times):
                str_times_dummy.append("%f  '%s_%i.txt'\n" % (
                    time, self.snapshot_basefilename, i + 1))

            str_times_dummy.append('#')
            d['str_times_snapshots'] = ''.join(str_times_dummy)
            d['n_snapshots'] = len(str_times_dummy) - 1
        else:
            d['str_times_snapshots'] = '# '
            d['n_snapshots'] = 0

        if self.sw_los_displacement == 1:
            d['str_los_vector'] = str_float_vals(self.los_vector)
        else:
            d['str_los_vector'] = ''

        if self.sw_coulomb_stress == 1:
            d['str_coulomb_master_field'] = \
                self.coulomb_master_field.string_for_config()
        else:
            d['str_coulomb_master_field'] = ''

        d['str_psgrn_outdir'] = "'%s'" % self.psgrn_outdir
        d['str_pscmp_outdir'] = "'%s'" % './'

        d['str_indispl_filenames'] = str_str_vals(self.indispl_filenames)
        d['str_instress_filenames'] = str_str_vals(self.instress_filenames)
        d['str_intilt_filenames'] = str_str_vals(self.intilt_filenames)
        d['str_ingravity_filenames'] = str_str_vals(self.ingravity_filenames)

        d['str_outdispl_filenames'] = str_str_vals(self.outdispl_filenames)
        d['str_outstress_filenames'] = str_str_vals(self.outstress_filenames)
        d['str_outtilt_filenames'] = str_str_vals(self.outtilt_filenames)
        d['str_outgravity_filenames'] = str_str_vals(self.outgravity_filenames)

        d['str_displ_sw_output_types'] = str_int_vals(
            self.displ_sw_output_types)
        d['str_stress_sw_output_types'] = str_int_vals(
            self.stress_sw_output_types)
        d['str_tilt_sw_output_types'] = str_int_vals(
            self.tilt_sw_output_types)
        d['str_gravity_sw_output_types'] = str_int_vals(
            self.gravity_sw_output_types)

        template = '''# autogenerated PSCMP input by pscmp.py
#===============================================================================
# This is input file of FORTRAN77 program "pscmp08" for modeling post-seismic
# deformation induced by earthquakes in multi-layered viscoelastic media using
# the Green's function approach. The earthquke source is represented by an
# arbitrary number of rectangular dislocation planes. For more details, please
# read the accompanying READ.ME file.
#
# written by Rongjiang Wang
# GeoForschungsZentrum Potsdam
# e-mail: wang@gfz-potsdam.de
# phone +49 331 2881209
# fax +49 331 2881204
#
# Last modified: Potsdam, July, 2008
#
# References:
#
# (1) Wang, R., F. Lorenzo-Martin and F. Roth (2003), Computation of deformation
#     induced by earthquakes in a multi-layered elastic crust - FORTRAN programs
#     EDGRN/EDCMP, Computer and Geosciences, 29(2), 195-207.
# (2) Wang, R., F. Lorenzo-Martin and F. Roth (2006), PSGRN/PSCMP - a new code for
#     calculating co- and post-seismic deformation, geoid and gravity changes
#     based on the viscoelastic-gravitational dislocation theory, Computers and
#     Geosciences, 32, 527-541. DOI:10.1016/j.cageo.2005.08.006.
# (3) Wang, R. (2005), The dislocation theory: a consistent way for including the
#     gravity effect in (visco)elastic plane-earth models, Geophysical Journal
#     International, 161, 191-196.
#
#################################################################
##                                                             ##
## Green's functions should have been prepared with the        ##
## program "psgrn08" before the program "pscmp08" is started.  ##
##                                                             ##
## For local Cartesian coordinate system, the Aki's convention ##
## is used, that is, x is northward, y is eastward, and z is   ##
## downward.                                                   ##
##                                                             ##
## If not specified otherwise, SI Unit System is used overall! ##
##                                                             ##
#################################################################
#===============================================================================
# OBSERVATION ARRAY
# =================
# 1. selection for irregular observation positions (= 0) or a 1D observation
#    profile (= 1) or a rectangular 2D observation array (= 2): iposrec
#
#    IF (iposrec = 0 for irregular observation positions) THEN
#
# 2. number of positions: nrec
#
# 3. coordinates of the observations: (lat(i),lon(i)), i=1,nrec
#
#    ELSE IF (iposrec = 1 for regular 1D observation array) THEN
#
# 2. number of position samples of the profile: nrec
#
# 3. the start and end positions: (lat1,lon1), (lat2,lon2)
#
#    ELSE IF (iposrec = 2 for rectanglular 2D observation array) THEN
#
# 2. number of x samples, start and end values: nxrec, xrec1, xrec2
#
# 3. number of y samples, start and end values: nyrec, yrec1, yrec2
#
#    sequence of the positions in output data: lat(1),lon(1); ...; lat(nx),lon(1);
#    lat(1),lon(2); ...; lat(nx),lon(2); ...; lat(1),lon(ny); ...; lat(nx),lon(ny).
#
#    Note that the total number of observation positions (nrec or nxrec*nyrec)
#    should be <= NRECMAX (see pecglob.h)!
#===============================================================================
 %(sw_observation_type)i
%(str_npoints)s
%(str_observation)s
#===============================================================================
# OUTPUTS
# =======
#
# 1. select output for los displacement (only for snapshots, see below), x, y,
#    and z-cosines to the INSAR orbit: insar (1/0 = yes/no), xlos, ylos, zlos
#
#    if this option is selected (insar = 1), the snapshots will include additional
#    data:
#    LOS_Dsp = los displacement to the given satellite orbit.
#
# 2. select output for Coulomb stress changes (only for snapshots, see below):
#    icmb (1/0 = yes/no), friction, Skempton ratio, strike, dip, and rake angles
#    [deg] describing the uniform regional master fault mechanism, the uniform
#    regional principal stresses: sigma1, sigma2 and sigma3 [Pa] in arbitrary
#    order (the orietation of the pre-stress field will be derived by assuming
#    that the master fault is optimally oriented according to Coulomb failure
#    criterion)
#
#    if this option is selected (icmb = 1), the snapshots will include additional
#    data:
#    CMB_Fix, Sig_Fix = Coulomb and normal stress changes on master fault;
#    CMB_Op1/2, Sig_Op1/2 = Coulomb and normal stress changes on the two optimally
#                       oriented faults;
#    Str_Op1/2, Dip_Op1/2, Slp_Op1/2 = strike, dip and rake angles of the two
#                       optimally oriented faults.
#
#    Note: the 1. optimally orieted fault is the one closest to the master fault.
#
# 3. output directory in char format: outdir
#
# 4. select outputs for displacement components (1/0 = yes/no): itout(i), i=1-3
#
# 5. the file names in char format for the x, y, and z components:
#    toutfile(i), i=1-3
#
# 6. select outputs for stress components (1/0 = yes/no): itout(i), i=4-9
#
# 7. the file names in char format for the xx, yy, zz, xy, yz, and zx components:
#    toutfile(i), i=4-9
#
# 8. select outputs for vertical NS and EW tilt components, block rotation, geoid
#    and gravity changes (1/0 = yes/no): itout(i), i=10-14
#
# 9. the file names in char format for the NS tilt (positive if borehole top
#    tilts to north), EW tilt (positive if borehole top tilts to east), block
#    rotation (clockwise positive), geoid and gravity changes: toutfile(i), i=10-14
#
#    Note that all above outputs are time series with the time window as same
#    as used for the Green's functions
#
#10. number of scenario outputs ("snapshots": spatial distribution of all above
#    observables at given time points; <= NSCENMAX (see pscglob.h): nsc
#
#11. the time [day], and file name (in char format) for the 1. snapshot;
#12. the time [day], and file name (in char format) for the 2. snapshot;
#13. ...
#
#    Note that all file or directory names should not be longer than 80
#    characters. Directories must be ended by / (unix) or \ (dos)!
#===============================================================================
 %(sw_los_displacement)i    %(str_los_vector)s
 %(sw_coulomb_stress)i    %(str_coulomb_master_field)s
 %(str_pscmp_outdir)s
 %(str_displ_sw_output_types)s
 %(str_outdispl_filenames)s
 %(str_stress_sw_output_types)s
 %(str_outstress_filenames)s
 %(str_tilt_sw_output_types)s    %(str_gravity_sw_output_types)s
 %(str_outtilt_filenames)s %(str_outgravity_filenames)s
 %(n_snapshots)i
%(str_times_snapshots)s
#===============================================================================
#
# GREEN'S FUNCTION DATABASE
# =========================
# 1. directory where the Green's functions are stored: grndir
#
# 2. file names (without extensions!) for the 13 Green's functions:
#    3 displacement komponents (uz, ur, ut): green(i), i=1-3
#    6 stress components (szz, srr, stt, szr, srt, stz): green(i), i=4-9
#    radial and tangential components measured by a borehole tiltmeter,
#    rigid rotation around z-axis, geoid and gravity changes (tr, tt, rot, gd, gr):
#    green(i), i=10-14
#
#    Note that all file or directory names should not be longer than 80
#    characters. Directories must be ended by / (unix) or \ (dos)! The
#    extensions of the file names will be automatically considered. They
#    are ".ep", ".ss", ".ds" and ".cl" denoting the explosion (inflation)
#    strike-slip, the dip-slip and the compensated linear vector dipole
#    sources, respectively.
#
#===============================================================================
 %(str_psgrn_outdir)s
 %(str_indispl_filenames)s
 %(str_instress_filenames)s
 %(str_intilt_filenames)s    %(str_ingravity_filenames)s
#===============================================================================
# RECTANGULAR SUBFAULTS
# =====================
# 1. number of subfaults (<= NSMAX in pscglob.h): ns
#
# 2. parameters for the 1. rectangular subfault: geographic coordinates
#    (O_lat, O_lon) [deg] and O_depth [km] of the local reference point on
#    the present fault plane, length (along strike) [km] and width (along down
#    dip) [km], strike [deg], dip [deg], number of equi-size fault patches along
#    the strike (np_st) and along the dip (np_di) (total number of fault patches
#    = np_st x np_di), and the start time of the rupture; the following data
#    lines describe the slip distribution on the present sub-fault:
#
#    pos_s[km]  pos_d[km]  slip_strike[m]  slip_downdip[m]  opening[m]
#
#    where (pos_s,pos_d) defines the position of the center of each patch in
#    the local coordinate system with the origin at the reference point:
#    pos_s = distance along the length (positive in the strike direction)
#    pos_d = distance along the width (positive in the down-dip direction)
#
#
# 3. ... for the 2. subfault ...
# ...
#                   N
#                  /
#                 /| strike
#                +------------------------
#                |\        p .            \ W
#                :-\      i .              \ i
#                |  \    l .                \ d
#                :90 \  S .                  \ t
#                |-dip\  .                    \ h
#                :     \. | rake               \
#                Z      -------------------------
#                              L e n g t h
#
#    Simulation of a Mogi source:
#    (1) Calculate deformation caused by three small openning plates (each
#        causes a third part of the volume of the point inflation) located
#        at the same depth as the Mogi source but oriented orthogonal to
#        each other.
#    (2) Multiply the results by 3(1-nu)/(1+nu), where nu is the Poisson
#        ratio at the source depth.
#    The multiplication factor is the ratio of the seismic moment (energy) of
#    the Mogi source to that of the plate openning with the same volume change.
#===============================================================================
# n_faults
#-------------------------------------------------------------------------------
 %(n_patches)i
#-------------------------------------------------------------------------------
# n   O_lat   O_lon   O_depth length  width strike dip   np_st np_di start_time
# [-] [deg]   [deg]   [km]    [km]     [km] [deg]  [deg] [-]   [-]   [day]
#     pos_s   pos_d   slp_stk slp_ddip open
#     [km]    [km]    [m]     [m]      [m]
#-------------------------------------------------------------------------------
%(patches_str)s
#================================end of input===================================
'''  # noqa
        return (template % d).encode('ascii')


class PsGrnPsCmpConfig(Object):
    '''
    Combined config Object of PsGrn and PsCmp.

    Note: attributes in this class use non-standard units [days] to be
    consistent with PSCMP text file input. Please read the documentation
    carefully.
    '''
    tmin_days = Float.T(
        default=0.,
        help='Min. time in days')
    tmax_days = Float.T(
        default=1.,
        help='Max. time in days')
    gf_outdir = String.T(default='psgrn_functions')

    psgrn_config = PsGrnConfig.T(default=PsGrnConfig.D())
    pscmp_config = PsCmpConfig.T(default=PsCmpConfig.D())


class PsCmpError(gf.store.StoreError):
    pass


class Interrupted(gf.store.StoreError):
    def __str__(self):
        return 'Interrupted.'


class PsCmpRunner(object):
    '''
    Wrapper object to execute the program fomosto_pscmp with the specified
    configuration.

    :param tmp: string, path to the temporary directy where calculation
        results are stored
    :param keep_tmp: boolean, if True the result directory is kept
    '''
    def __init__(self, tmp=None, keep_tmp=False):
        if tmp is not None:
            tmp = os.path.abspath(tmp)
        self.tempdir = mkdtemp(prefix='pscmprun-', dir=tmp)
        self.keep_tmp = keep_tmp
        self.config = None

    def run(self, config):
        '''
        Run the program!

        :param config: :py:class:`PsCmpConfigFull`
        '''
        self.config = config

        input_fn = pjoin(self.tempdir, 'input')

        with open(input_fn, 'wb') as f:
            input_str = config.string_for_config()

            logger.debug('===== begin pscmp input =====\n'
                         '%s===== end pscmp input =====' % input_str.decode())

            f.write(input_str)

        program = program_bins['pscmp.%s' % config.version]

        old_wd = os.getcwd()
        os.chdir(self.tempdir)

        interrupted = []

        def signal_handler(signum, frame):
            os.kill(proc.pid, signal.SIGTERM)
            interrupted.append(True)

        original = signal.signal(signal.SIGINT, signal_handler)
        try:
            try:
                proc = Popen(program, stdin=PIPE, stdout=PIPE, stderr=PIPE,
                             close_fds=True)

            except OSError as err:
                os.chdir(old_wd)
                logger.error('OS error: {0}'.format(err))
                raise PsCmpError(
                    '''could not start pscmp executable: "%s"
Available fomosto backends and download links to the modelling codes are listed
on

      https://pyrocko.org/docs/current/apps/fomosto/backends.html

''' % program)

            (output_str, error_str) = proc.communicate(b'input\n')

        finally:
            signal.signal(signal.SIGINT, original)

        if interrupted:
            raise KeyboardInterrupt()

        logger.debug('===== begin pscmp output =====\n'
                     '%s===== end pscmp output =====' % output_str.decode())

        errmsg = []
        if proc.returncode != 0:
            errmsg.append(
                'pscmp had a non-zero exit state: %i' % proc.returncode)

        if error_str:
            errmsg.append('pscmp emitted something via stderr')

        if output_str.lower().find(b'error') != -1:
            errmsg.append("the string 'error' appeared in pscmp output")

        if errmsg:
            self.keep_tmp = True

            os.chdir(old_wd)
            raise PsCmpError('''
===== begin pscmp input =====
{pscmp_input}===== end pscmp input =====
===== begin pscmp output =====
{pscmp_output}===== end pscmp output =====
===== begin pscmp error =====
{pscmp_error}===== end pscmp error =====
{error_messages}
pscmp has been invoked as "{call}"
in the directory {dir}'''.format(
                pscmp_input=input_str,
                pscmp_output=output_str,
                pscmp_error=error_str,
                error_messages='\n'.join(errmsg),
                call=program,
                dir=self.tempdir)
                .lstrip())

        self.pscmp_output = output_str
        self.pscmp_error = error_str

        os.chdir(old_wd)

    def get_results(self, component='displ'):
        '''
        Get the resulting components from the stored directory.
        Be careful: The z-component is downward positive!

        :param component: string, the component to retrieve from the
        result directory, may be:
            "displ": displacement, n x 3 array
            "stress": stresses n x 6 array
            "tilt': tilts n x 3 array,
            "gravity': gravity n x 2 array
            "all": all the above together
        '''
        assert self.config.snapshots is not None
        fns = self.config.get_output_filenames(self.tempdir)

        output = []
        for fn in fns:
            if not os.path.exists(fn):
                continue

            data = num.loadtxt(fn, skiprows=1, dtype=float)

            try:
                _, idxs = pscmp_component_mapping[component]
            except KeyError:
                raise Exception('component %s not supported! Either: %s' % (
                    component, ', '.join(
                        '"%s"' % k for k in pscmp_component_mapping.keys())))

            istart, iend = idxs

            output.append(data[:, istart:iend])

        return output

    def get_traces(self, component='displ'):
        '''
        Load snapshot data array and return specified components.
        Transform array component and receiver wise to list of
        :py:class:`pyrocko.trace.Trace`
        '''

        distances = self.config.observation.distances

        output_list = self.get_results(component=component)
        deltat = self.config.snapshots.deltat

        nrec, ncomp = output_list[0].shape

        outarray = num.dstack([num.zeros([nrec, ncomp])] + output_list)

        comps, _ = pscmp_component_mapping[component]

        outtraces = []
        for row in range(nrec):
            for col, comp in enumerate(comps):
                tr = trace.Trace(
                    '', '%04i' % row, '', comp,
                    tmin=0.0, deltat=deltat, ydata=outarray[row, col, :],
                    meta=dict(distance=distances[row]))
                outtraces.append(tr)

        return outtraces

    def __del__(self):
        if self.tempdir:
            if not self.keep_tmp:
                shutil.rmtree(self.tempdir)
                self.tempdir = None
            else:
                logger.warning(
                    'not removing temporary directory: %s' % self.tempdir)


class PsGrnCmpGFBuilder(gf.builder.Builder):
    nsteps = 2

    def __init__(self, store_dir, step, shared, block_size=None, tmp=None,
                 force=False):

        self.store = gf.store.Store(store_dir, 'w')

        storeconf = self.store.config

        dummy_lat = 10.0
        dummy_lon = 10.0

        depths = storeconf.coords[0]
        lats = num.ones_like(depths) * dummy_lat
        lons = num.ones_like(depths) * dummy_lon
        points = num.vstack([lats, lons, depths]).T

        self.shear_moduli = storeconf.get_shear_moduli(
            lat=dummy_lat,
            lon=dummy_lon,
            points=points,
            interpolation='multilinear')

        self.lambda_moduli = storeconf.get_lambda_moduli(
            lat=dummy_lat,
            lon=dummy_lon,
            points=points,
            interpolation='multilinear')

        if step == 0:
            block_size = (1, storeconf.nsource_depths, storeconf.ndistances)
        else:
            if block_size is None:
                block_size = (1, 1, storeconf.ndistances)

        if len(storeconf.ns) == 2:
            block_size = block_size[1:]

        gf.builder.Builder.__init__(
            self, storeconf, step, block_size=block_size, force=force)

        baseconf = self.store.get_extra('psgrn_pscmp')

        cg = PsGrnConfigFull(**baseconf.psgrn_config.items())
        if cg.n_time_samples is None or cg.max_time is None:
            deltat_days = 1. / storeconf.sample_rate / day

            cg.n_time_samples = int(baseconf.tmax_days // deltat_days)
            cg.max_time = baseconf.tmax_days
        else:
            warnings.warn(
                'PsGrnConfig is defining n_times_samples and max_time,'
                ' this is replaced by PsGrnPsCmpConfig tmin and tmax.',
                FutureWarning)

        cc = PsCmpConfigFull(**baseconf.pscmp_config.items())
        if cc.snapshots is None:
            deltat_days = 1. / storeconf.sample_rate / day

            cc.snapshots = PsCmpSnapshots(
                tmin=baseconf.tmin_days,
                tmax=baseconf.tmax_days,
                deltatdays=deltat_days)
        else:
            warnings.warn(
                'PsCmpConfig is defining snapshots,'
                ' this is replaced by PsGrnPsCmpConfig tmin and tmax.',
                FutureWarning)

        cg.earthmodel_1d = storeconf.earthmodel_1d

        gf_outpath = os.path.join(store_dir, baseconf.gf_outdir)

        cg.psgrn_outdir = gf_outpath + '/'
        cc.psgrn_outdir = gf_outpath + '/'

        util.ensuredir(gf_outpath)

        self.cg = cg
        self.cc = cc

    def cleanup(self):
        self.store.close()

    def work_block(self, iblock):

        if len(self.store.config.ns) == 2:
            (sz, firstx), (sz, lastx), (ns, nx) = \
                self.get_block_extents(iblock)
            mu = self.shear_moduli[iblock]
            lame_lambda = self.lambda_moduli[iblock]

            rz = self.store.config.receiver_depth
        else:
            (rz, sz, firstx), (rz, sz, lastx), (nr, ns, nx) = \
                self.get_block_extents(iblock)

        fc = self.store.config
        cc = copy.deepcopy(self.cc)
        cg = copy.deepcopy(self.cg)

        dx = fc.distance_delta

        logger.info(
            'Starting step %i / %i, block %i / %i' %
            (self.step + 1, self.nsteps, iblock + 1, self.nblocks))

        if self.step == 0:
            if cg.gf_depth_spacing is None:
                gf_depth_spacing = fc.source_depth_delta
            else:
                gf_depth_spacing = cg.gf_depth_spacing * km

            n_steps_depth = int((fc.source_depth_max - fc.source_depth_min) /
                                gf_depth_spacing) + 1

            if cg.gf_distance_spacing is None:
                gf_distance_spacing = fc.distance_delta
            else:
                gf_distance_spacing = cg.gf_distance_spacing * km
            n_steps_distance = int((fc.distance_max - fc.distance_min) /
                                   gf_distance_spacing) + 1

            cg.depth_grid = PsGrnSpatialSampling(
                n_steps=n_steps_depth,
                start_distance=fc.source_depth_min / km,
                end_distance=fc.source_depth_max / km)

            cg.distance_grid = PsGrnSpatialSampling(
                n_steps=n_steps_distance,
                start_distance=fc.distance_min / km,
                end_distance=fc.distance_max / km)

            runner = PsGrnRunner(outdir=self.cg.psgrn_outdir)
            runner.run(cg, force=self.force)

        else:
            distances = num.linspace(
                firstx, firstx + (nx - 1) * dx, nx).tolist()

            # fomosto sample rate in s, pscmp takes days
            deltatdays = 1. / (fc.sample_rate * 24. * 3600.)
            cc.snapshots = PsCmpSnapshots(
                tmin=0., tmax=cg.max_time, deltatdays=deltatdays)
            cc.observation = PsCmpProfile(
                slat=0. - 0.001 * cake.m2d,
                slon=0.0,
                elat=0. + distances[-1] * cake.m2d,
                elon=0.0,
                n_steps=len(distances),
                distances=distances)

            runner = PsCmpRunner(keep_tmp=False)

            mtsize = float(dx * cc.rectangular_fault_size_factor)

            Ai = 1. / num.power(mtsize, 2)
            nullf, sf = get_iso_scaling_factors(mu=mu, lame_lambda=lame_lambda)

            mui = 1. / mu

            gfmapping = [
                (('nn',),
                 {'un': (0, Ai * sf), 'ud': (5, Ai * sf)}),
                (('ne',),
                 {'ue': (3, 1 * Ai * mui)}),
                (('nd',),
                 {'un': (1, 1 * Ai * mui), 'ud': (6, 1 * Ai * mui)}),
                (('ed',),
                 {'ue': (4, 1 * Ai * mui)}),
                (('dd',),
                 {'un': (2, Ai * sf), 'ud': (7, Ai * sf)}),
                (('ee',),
                 {'un': (8, Ai * sf), 'ud': (9, Ai * sf)}),
                ]

            for mt, gfmap in gfmapping:
                cc.rectangular_source_patches = []
                for idx in mt:
                    pmt = PsCmpMomentTensor(
                        lat=0. + 0.001 * dx * cake.m2d,
                        lon=0.0,
                        depth=float(sz),
                        width=mtsize,
                        length=mtsize,
                        idx=idx)

                    cc.rectangular_source_patches.extend(
                        pmt.to_rfs(nullf))

                runner.run(cc)

                rawtraces = runner.get_traces()

                interrupted = []

                def signal_handler(signum, frame):
                    interrupted.append(True)

                original = signal.signal(signal.SIGINT, signal_handler)
                self.store.lock()
                duplicate_inserts = 0

                try:
                    for itr, tr in enumerate(rawtraces):
                        if tr.channel in gfmap:
                            x = tr.meta['distance']
                            ig, factor = gfmap[tr.channel]

                            if len(self.store.config.ns) == 2:
                                args = (sz, x, ig)
                            else:
                                args = (rz, sz, x, ig)

                            gf_tr = gf.store.GFTrace.from_trace(tr)

                            gf_tr.data *= factor

                            try:
                                self.store.put(args, gf_tr)
                            except gf.store.DuplicateInsert:
                                duplicate_inserts += 1

                finally:
                    if duplicate_inserts:
                        logger.warning(
                            '%i insertions skipped (duplicates)'
                            % duplicate_inserts)

                    self.store.unlock()
                    signal.signal(signal.SIGINT, original)

                if interrupted:
                    raise KeyboardInterrupt()

        logger.info(
            'Done with step %i / %i, block %i / %i' % (
                self.step + 1, self.nsteps, iblock + 1, self.nblocks))


def init(store_dir, variant):
    if variant is None:
        variant = '2008a'

    if ('pscmp.' + variant) not in program_bins:
        raise gf.store.StoreError('unsupported pscmp variant: %s' % variant)

    if ('psgrn.' + variant) not in program_bins:
        raise gf.store.StoreError('unsupported psgrn variant: %s' % variant)

    c = PsGrnPsCmpConfig()

    store_id = os.path.basename(os.path.realpath(store_dir))

    # Initialising a viscous mantle
    cake_mod = cake.load_model(fn=None, crust2_profile=(54., 23.))
    mantle = cake_mod.material(z=45*km)
    mantle.burger_eta1 = 5e17
    mantle.burger_eta2 = 1e19
    mantle.burger_alpha = 1.

    config = gf.meta.ConfigTypeA(
        id=store_id,
        ncomponents=10,
        sample_rate=1. / (3600. * 24.),
        receiver_depth=0. * km,
        source_depth_min=0. * km,
        source_depth_max=15. * km,
        source_depth_delta=.5 * km,
        distance_min=0. * km,
        distance_max=50. * km,
        distance_delta=1. * km,
        earthmodel_1d=cake_mod,
        modelling_code_id='psgrn_pscmp.%s' % variant,
        tabulated_phases=[])  # dummy list

    c.validate()
    config.validate()
    return gf.store.Store.create_editables(
        store_dir,
        config=config,
        extra={'psgrn_pscmp': c})


def build(store_dir,
          force=False,
          nworkers=None,
          continue_=False,
          step=None,
          iblock=None):

    return PsGrnCmpGFBuilder.build(
        store_dir, force=force, nworkers=nworkers, continue_=continue_,
        step=step, iblock=iblock)
