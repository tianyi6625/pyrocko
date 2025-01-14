import os

from pyrocko import gf
from pyrocko.gf import tractions, ws, LocalEngine
from pyrocko.plot import dynamic_rupture


# The store we are going extract data from:
store_id = 'iceland_reg_v2'

# First, download a Greens Functions store. If you already have one that you
# would like to use, you can skip this step and point the *store_superdirs* in
# the next step to that directory.

if not os.path.exists(store_id):
    ws.download_gf_store(site='kinherd', store_id=store_id)

# We need a pyrocko.gf.Engine object which provides us with the traces
# extracted from the store. In this case we are going to use a local
# engine since we are going to query a local store.
engine = LocalEngine(store_superdirs=['.'], default_store_id=store_id)

# The dynamic parameter used for discretization of the PseudoDynamicRupture are
# extracted from the stores config file.
store = engine.get_store(store_id)

# Define the traction structure as a composition of a homogeneous traction and
# a rectangular taper tapering the traction at the edges of the rupture
tracts = tractions.TractionComposition(
    components=[
        tractions.HomogeneousTractions(
            strike=1.e6,
            dip=0.,
            normal=0.),
        tractions.RectangularTaper()])

# Let's define the source now with its extension, orientation etc.
source = gf.PseudoDynamicRupture(
    lat=6.45,
    lon=37.06,
    length=30000.,
    width=10000.,
    strike=215.,
    dip=45.,
    anchor='top',
    gamma=0.6,
    depth=2000.,
    nucleation_x=0.25,
    nucleation_y=-0.5,
    nx=20,
    ny=10,
    pure_shear=True,
    tractions=tracts)

# The define PseudoDynamicSource needs to be divided into finite fault elements
# which is done using spacings defined by the greens function data base
source.discretize_patches(store)

# Define the rupture map object parameters as image center coordinates,
# radius in m and the size of the image
map_kwargs = dict(
    lat=6.50,
    lon=37.06,
    radius=20000.,
    width=25.,
    height=25.,
    source=source,
    show_topo=True)

# Initialize the map with the set arguments and display the traction vector
# length per patch.
m = dynamic_rupture.RuptureMap(**map_kwargs)
m.draw_patch_parameter('traction', cbar=True, anchor='top_right')
m.draw_nucleation_point()
m.save('traction_map.png')

# Initialize the map and generate a more complex plot of the dislocation at
# 3 s after origin time with the corresponding dislocation contour lines with
# a contour line at 0.15 m total dislocation. Also the belonging rupture front
# contour at 3 s is displayed together with nucleation point.
m = dynamic_rupture.RuptureMap(**map_kwargs)
m.draw_dislocation(time=3, cmap='summer')
m.draw_dislocation_vector(time=3, S='i15.', I='x20')
m.draw_time_contour(store, clevel=[3])
m.draw_dislocation_contour(time=3, clevel=[0.15])
m.draw_nucleation_point()
m.save('dislocation_map_3s.png')
