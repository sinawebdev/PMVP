"""
Payrolla animated logo -- Blender 5.x build script (v2, folded-ribbon mark).

The source of truth for the logo animation (with logo_parts.py beside it,
which holds the geometry). Two ways to run it:

* `blender --python scripts/logo/payrolla_logo_anim.py` opens Blender with
  the scene "Payrolla_Logo" built as the 1920x1080 film, ready to render.
  (Pasting it into the Scripting tab no longer works: it imports logo_parts.)
* Headless, through scripts/logo/build_logo_assets.py, which sets the
  PAYROLLA_LOGO_* environment variables below and renders each variant the
  web app and the desktop splash ship.

Production brief (30 fps, 5 s, frames 0-149, transparent background, camera
never moves):
  0-15    ribbon strip and stem start apart, off-axis, fading in, 98% scale
  15-45   both sweep inward toward their final positions
  20-70   the ribbon winds into the bowl (length-preserving curl)
  48-75   the stem half-twists 180 degrees, turning its darker back side
          to camera: this is the fold. P structurally complete at 75
  45-95   final alignment with a slight overshoot (no bounce)
  90-105  scale eases to 102%, 105-120 settles to 100%
  105-120 "Payrolla" wordmark fades and slides up into place
  120-149 perfect hold: nothing moves after frame 120
"""

import math
import os
import sys

import bpy
from mathutils import Vector

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from logo_parts import (  # noqa: E402
    MARK_H, MARK_W, OFF, TOTAL_TURN, key, lin, ribbon_coords, stem_outline,
)


# ------------------------------------------------------------------ knobs
def _knob(name, default, allowed=None):
    value = os.environ.get("PAYROLLA_LOGO_" + name, default).strip()
    if allowed and value not in allowed:
        raise ValueError(f"PAYROLLA_LOGO_{name}={value!r}; expected one of {sorted(allowed)}")
    return value


LAYOUT = _knob("LAYOUT", "stacked", {"stacked", "horizontal", "mark"})  # "mark": no wordmark
GROUND = _knob("GROUND", "light", {"light", "dark"})    # background the result sits on
FRAMING = _knob("FRAMING", "film", {"film", "asset"})   # "asset": square region, see below
PX_PER_UNIT = float(_knob("PX_PER_UNIT", "60"))         # asset framing only
SAMPLES = int(_knob("SAMPLES", "64"))
OUTPUT = _knob("OUT", "//render/payrolla_logo_")

# Light ground is the master palette. Dark ground lifts it exactly as
# payrolla-logo-dark.svg lifts the flat mark: the master's #0D4D4D start is
# the colour of the deep-teal surfaces, so on dark it reads as a hole.
PALETTES = {
    "light": {"front": ("#0D4D4D", "#17C3B2"), "back": "#0B4A48",
              "edge": "#7FE3D8", "word": "#0D4D4D"},
    "dark": {"front": ("#17C3B2", "#8FF0E4"), "back": "#135F5A",
             "edge": "#CFF8F2", "word": "#FFFFFF"},
}
PAL = PALETTES[GROUND]
BRIGHT_TEAL = "#17C3B2"

# Shading. The v1 render was fully lit and the lights pushed every hex off
# brand (#17C3B2 rendered as #40F5DC). Now most of the surface is unlit
# emission, which lands on the hex exactly under the Standard view transform,
# and LIT_SHARE of it still answers the lights so the curl and the fold read
# as 3D. EMISSION_GAIN offsets the light that share adds at the final pose.
LIT_SHARE = 0.25
EMISSION_GAIN = 0.92

FPS = 30
FIRST, LAST = 0, 149      # 150 frames = exactly 5.0 s
T = 0.12                  # ribbon thickness, in ribbon widths
SCENE_NAME = "Payrolla_Logo"

# Asset framing: a REGION_W x REGION_H window (ribbon units) centred on
# (REGION_CX, REGION_CY) relative to the finished mark's centre must hold
# every frame of the motion -- the unwound ribbon starts far above the
# finished P. build_logo_assets.py crops to what moved and refuses a render
# whose motion touches the edge. The camera distance is the film's, so every
# variant has the same perspective.
REGION_W = float(_knob("REGION_W", "10"))
REGION_H = float(_knob("REGION_H", "10"))
REGION_CX = float(_knob("REGION_CX", "0"))
REGION_CY = float(_knob("REGION_CY", "1.75"))
CAM_DISTANCE = 41.3


def blend_alpha(mat):
    """True alpha blending for the fade-ins. Eevee's default (dithered)
    transparency renders partial alpha as noise, which read as sandpaper
    texture through the first second. Every piece is a closed solid, so
    culling back faces and drawing only the front layer keeps a half-faded
    ribbon from showing its own far side through itself."""
    if hasattr(mat, "surface_render_method"):
        mat.surface_render_method = "BLENDED"
    else:
        mat.blend_method = "BLEND"
    mat.use_backface_culling = True
    if hasattr(mat, "use_transparency_overlap"):
        mat.use_transparency_overlap = False


# ------------------------------------------------------------------ scene
old = bpy.data.scenes.get(SCENE_NAME)
if old:
    for ob in list(old.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    bpy.data.scenes.remove(old)

scene = bpy.data.scenes.new(SCENE_NAME)
if bpy.context.window:
    bpy.context.window.scene = scene

r = scene.render
r.fps = FPS
scene.frame_start, scene.frame_end = FIRST, LAST
r.resolution_x, r.resolution_y = 1920, 1080
r.resolution_percentage = 100
for engine in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"):
    try:
        r.engine = engine
        break
    except TypeError:
        pass
r.film_transparent = True
r.use_motion_blur = False
r.image_settings.file_format = "PNG"
r.image_settings.color_mode = "RGBA"
r.filepath = OUTPUT
scene.view_settings.view_transform = "Standard"   # keeps the brand hexes true
scene.view_settings.look = "None"
try:
    scene.eevee.taa_render_samples = SAMPLES
except AttributeError:
    pass

world = bpy.data.worlds.new("Payrolla_World")      # lighting only; film is transparent
scene.world = world
world.use_nodes = True
world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.9, 0.95, 0.95, 1)
world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.2


def add_modifiers(ob):
    weld = ob.modifiers.new("Clean", "WELD")
    weld.merge_threshold = 0.0005
    sol = ob.modifiers.new("Thickness", "SOLIDIFY")
    sol.thickness = T
    sol.offset = -1.0
    sol.use_even_offset = True
    bev = ob.modifiers.new("Edge", "BEVEL")
    bev.width = 0.018
    bev.segments = 4
    bev.limit_method = "ANGLE"
    bev.harden_normals = True


# ------------------------------------------------------------------ materials
def ribbon_material(name, front_is_positive_z, alpha_nodes):
    """Front face: diagonal gradient locked to the mark. Back face: darker
    teal. Bevelled edges: lighter highlight. Mostly unlit; see LIT_SHARE."""
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    blend_alpha(mat)
    nt = mat.node_tree
    nt.nodes.clear()
    N = nt.nodes.new
    L = nt.links.new
    out = N("ShaderNodeOutputMaterial")
    tc = N("ShaderNodeTexCoord")
    tc.object = mark_root                      # gradient travels with the logo
    nrm = N("ShaderNodeSeparateXYZ")
    # gradient coordinate: bottom-left deep -> top-right bright
    diag = N("ShaderNodeVectorMath")
    diag.operation = "DOT_PRODUCT"
    diag.inputs[1].default_value = (0.45, 0.89, 0.0)
    fac = N("ShaderNodeMapRange")
    fac.inputs["From Min"].default_value = -1.9
    fac.inputs["From Max"].default_value = 1.9
    ramp = N("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].position = 0.1
    ramp.color_ramp.elements[0].color = lin(PAL["front"][0])
    ramp.color_ramp.elements[1].position = 0.8
    ramp.color_ramp.elements[1].color = lin(PAL["front"][1])
    L(tc.outputs["Object"], diag.inputs[0])
    L(diag.outputs["Value"], fac.inputs["Value"])
    L(fac.outputs["Result"], ramp.inputs["Fac"])
    # which side faces the camera (object-space normal z)
    L(tc.outputs["Normal"], nrm.inputs[0])
    side = N("ShaderNodeMath")
    side.operation = "GREATER_THAN" if front_is_positive_z else "LESS_THAN"
    side.inputs[1].default_value = 0.0
    L(nrm.outputs["Z"], side.inputs[0])
    face_mix = N("ShaderNodeMix")
    face_mix.data_type = "RGBA"
    face_mix.inputs["A"].default_value = lin(PAL["back"])
    L(side.outputs[0], face_mix.inputs["Factor"])
    L(ramp.outputs["Color"], face_mix.inputs["B"])
    # edge highlight on the bevel/side walls
    absz = N("ShaderNodeMath")
    absz.operation = "ABSOLUTE"
    L(nrm.outputs["Z"], absz.inputs[0])
    edge = N("ShaderNodeMapRange")
    edge.inputs["From Min"].default_value = 0.9
    edge.inputs["From Max"].default_value = 0.2
    L(absz.outputs[0], edge.inputs["Value"])
    edge_mix = N("ShaderNodeMix")
    edge_mix.data_type = "RGBA"
    edge_mix.inputs["B"].default_value = lin(PAL["edge"])
    L(edge.outputs["Result"], edge_mix.inputs["Factor"])
    L(face_mix.outputs["Result"], edge_mix.inputs["A"])
    colour = edge_mix.outputs["Result"]
    # mostly unlit emission, with LIT_SHARE answering the lights. Diffuse only:
    # specular and coat add white on top of the colour, which lifted the near-
    # zero red channel of every teal by 10-18 levels and greyed the stem.
    bsdf = N("ShaderNodeBsdfPrincipled")
    L(colour, bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = 0.38
    bsdf.inputs["Specular IOR Level"].default_value = 0.0
    emit = N("ShaderNodeEmission")
    L(colour, emit.inputs["Color"])
    emit.inputs["Strength"].default_value = EMISSION_GAIN
    shade = N("ShaderNodeMixShader")
    shade.inputs["Fac"].default_value = LIT_SHARE
    L(emit.outputs[0], shade.inputs[1])
    L(bsdf.outputs["BSDF"], shade.inputs[2])
    # fade in: transparent -> shaded
    alpha = N("ShaderNodeValue")
    alpha.name = "Reveal"
    alpha.outputs[0].default_value = 1.0
    clear = N("ShaderNodeBsdfTransparent")
    reveal = N("ShaderNodeMixShader")
    L(alpha.outputs[0], reveal.inputs["Fac"])
    L(clear.outputs[0], reveal.inputs[1])
    L(shade.outputs[0], reveal.inputs[2])
    L(reveal.outputs[0], out.inputs["Surface"])
    alpha_nodes.append(nt)
    return mat


# ------------------------------------------------------------------ build mark
mark_root = bpy.data.objects.new("Payrolla_Mark", None)
mark_root.empty_display_type = "PLAIN_AXES"
scene.collection.objects.link(mark_root)

alpha_trees = []

# ribbon: one strip mesh; absolute shape keys hold the winding stages
N_STAGES = 9
START_TURN = math.radians(35)            # starts gently bent, not ruler-straight
stages = [START_TURN + (TOTAL_TURN - START_TURN) * i / (N_STAGES - 1) for i in range(N_STAGES)]
co0 = ribbon_coords(stages[0])
n_pairs = len(co0) // 2
faces = [(2 * i, 2 * i + 1, 2 * i + 3, 2 * i + 2) for i in range(n_pairs - 1)]
rmesh = bpy.data.meshes.new("Ribbon")
rmesh.from_pydata(co0, [], faces)
rmesh.update()
ribbon = bpy.data.objects.new("Ribbon", rmesh)
scene.collection.objects.link(ribbon)
ribbon.parent = mark_root
ribbon.shape_key_add(name="Stage_0", from_mix=False)
for i, turn in enumerate(stages[1:], start=1):
    kb = ribbon.shape_key_add(name=f"Stage_{i}", from_mix=False)
    for v, c in zip(kb.data, ribbon_coords(turn)):
        v.co = c
rmesh.shape_keys.use_relative = False
for kb in rmesh.shape_keys.key_blocks:
    kb.interpolation = "KEY_LINEAR"
EVAL_MAX = rmesh.shape_keys.key_blocks[-1].frame
add_modifiers(ribbon)
ribbon.data.materials.append(ribbon_material("Ribbon_Front", True, alpha_trees))


# stem: leaf shape below the column, pivoting on its own vertical centreline
pivot = Vector((0.5, -1.7)) + OFF.to_2d()
spts = [(p.x + OFF.x - pivot.x, p.y + OFF.y - pivot.y, T / 2) for p in stem_outline()]
smesh = bpy.data.meshes.new("Stem")
smesh.from_pydata(spts, [], [list(range(len(spts)))])
smesh.update()
stem = bpy.data.objects.new("Stem", smesh)
scene.collection.objects.link(stem)
stem.parent = mark_root
stem.location = (pivot.x, pivot.y, -T / 2)
add_modifiers(stem)
# the stem shows the ribbon's BACK once twisted into place
stem.data.materials.append(ribbon_material("Stem_BackSide", False, alpha_trees))


# ------------------------------------------------------------------ wordmark
def load_satoshi():
    """Finds Satoshi however it was installed (all users or just you, static
    or variable file), preferring the Bold cut the brand sheet uses."""
    dirs = [r"C:\Windows\Fonts",
            os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Microsoft\Windows\Fonts")]
    found = []
    for d in dirs:
        if os.path.isdir(d):
            found += [os.path.join(d, f) for f in os.listdir(d)
                      if f.lower().startswith("satoshi") and f.lower().endswith((".otf", ".ttf"))]
    if not found:
        return None
    rank = lambda p: (0 if "bold" in p.lower() and "extra" not in p.lower() and "italic" not in p.lower()
                      else 1 if "variable" in p.lower() and "italic" not in p.lower() else 2)
    return bpy.data.fonts.load(sorted(found, key=rank)[0], check_existing=True)


word = wmat = font = None
if LAYOUT != "mark":
    font = load_satoshi()
    tdata = bpy.data.curves.new("Wordmark", type="FONT")
    tdata.body = "Payrolla"
    tdata.extrude = 0.02
    tdata.align_x = "CENTER" if LAYOUT == "stacked" else "LEFT"
    tdata.align_y = "TOP" if LAYOUT == "stacked" else "CENTER"
    if font:
        tdata.font = font
    word = bpy.data.objects.new("Wordmark", tdata)
    scene.collection.objects.link(word)

    wmat = bpy.data.materials.new("Wordmark")
    wmat.use_nodes = True
    blend_alpha(wmat)
    wnt = wmat.node_tree
    wnt.nodes.clear()
    w_out = wnt.nodes.new("ShaderNodeOutputMaterial")
    w_emit = wnt.nodes.new("ShaderNodeEmission")        # unlit: renders the exact hex
    w_emit.inputs["Color"].default_value = lin(PAL["word"])
    w_clear = wnt.nodes.new("ShaderNodeBsdfTransparent")
    w_mix = wnt.nodes.new("ShaderNodeMixShader")
    walpha = wnt.nodes.new("ShaderNodeValue")
    walpha.name = "Reveal"
    wnt.links.new(walpha.outputs[0], w_mix.inputs["Fac"])
    wnt.links.new(w_clear.outputs[0], w_mix.inputs[1])
    wnt.links.new(w_emit.outputs[0], w_mix.inputs[2])
    wnt.links.new(w_mix.outputs[0], w_out.inputs["Surface"])
    tdata.materials.append(wmat)

    # size the wordmark relative to the mark (ratios read off the brand sheet)
    tdata.size = 1.0
    dg = scene.view_layers[0].depsgraph
    dg.update()
    raw_w = word.evaluated_get(dg).dimensions.x or 4.0
    target_w = MARK_W * (1.9 if LAYOUT == "stacked" else 3.45)
    tdata.size = target_w / raw_w

    if LAYOUT == "stacked":
        word_final = Vector((0.0, -MARK_H / 2 - 0.55, 0.0))
    else:
        word_final = Vector((MARK_W / 2 + 0.6, 0.35, 0.0))
    word.location = word_final

# ------------------------------------------------------------------ animation
# 1) fade in
for nt in alpha_trees:
    key(nt, 'nodes["Reveal"].outputs[0].default_value', 0, 0.0)
    key(nt, 'nodes["Reveal"].outputs[0].default_value', 22, 1.0)

# 2) whole mark: off-axis and 98% -> sweep in -> overshoot settle -> 102% -> 100%
rot0 = (math.radians(16), math.radians(-26), math.radians(9))
rot45 = (math.radians(3), math.radians(-5), math.radians(1.5))
for i in range(3):
    key(mark_root, "rotation_euler", 0, rot0[i], i)
    key(mark_root, "rotation_euler", 15, rot0[i], i)
    key(mark_root, "rotation_euler", 45, rot45[i], i, out="BACK", ease="EASE_OUT", back=1.1)
    key(mark_root, "rotation_euler", 95, 0.0, i)
start_loc = (-0.9, 0.5, 0.0)
for i in range(3):
    key(mark_root, "location", 0, start_loc[i], i)
    key(mark_root, "location", 15, start_loc[i], i)
    key(mark_root, "location", 45, 0.0, i)
for i in range(3):
    key(mark_root, "scale", 0, 0.98, i)
    key(mark_root, "scale", 90, 1.0, i)
    key(mark_root, "scale", 105, 1.02, i)
    key(mark_root, "scale", 120, 1.0, i)

# 3) the ribbon winds into the bowl
key(rmesh.shape_keys, "eval_time", 20, 0.0)
key(rmesh.shape_keys, "eval_time", 70, EVAL_MAX)

# 4) the stem: separated -> joins -> half-twist (the fold)
stem_start = (stem.location.x - 0.55, stem.location.y - 0.8, stem.location.z + 0.25)
for i in range(3):
    key(stem, "location", 0, stem_start[i], i)
    key(stem, "location", 15, stem_start[i], i)
    key(stem, "location", 48, (pivot.x, pivot.y, -T / 2)[i], i)
key(stem, "rotation_euler", 0, math.pi, 1)
key(stem, "rotation_euler", 48, math.pi, 1)
key(stem, "rotation_euler", 75, 0.0, 1)

# 5) wordmark
if word is not None:
    key(wmat.node_tree, 'nodes["Reveal"].outputs[0].default_value', 0, 0.0)
    key(wmat.node_tree, 'nodes["Reveal"].outputs[0].default_value', 105, 0.0)
    key(wmat.node_tree, 'nodes["Reveal"].outputs[0].default_value', 120, 1.0)
    key(word, "location", 0, word_final.y - 0.18, 1)
    key(word, "location", 105, word_final.y - 0.18, 1, ease="EASE_OUT")
    key(word, "location", 120, word_final.y, 1)

# ------------------------------------------------------------------ camera (never moves)
scene.frame_set(LAST)
dg = scene.view_layers[0].depsgraph
dg.update()
xs, ys = [-MARK_W / 2, MARK_W / 2], [-MARK_H / 2, MARK_H / 2]
if word is not None:
    wev = word.evaluated_get(dg)
    for c in wev.bound_box:
        p = wev.matrix_world @ Vector(c)
        xs.append(p.x)
        ys.append(p.y)
cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
bw, bh = max(xs) - min(xs), max(ys) - min(ys)
frame_w = max(bw / 0.5, (bh / 0.6) * 16 / 9)       # logo gets room to breathe

aim = bpy.data.objects.new("Aim", None)
aim.location = (cx, cy, 0)
scene.collection.objects.link(aim)

cam_data = bpy.data.cameras.new("Payrolla_Cam")
cam_data.lens = 85                                    # mild perspective, near-flat
cam_data.clip_end = 500
cam = bpy.data.objects.new("Payrolla_Cam", cam_data)
scene.collection.objects.link(cam)
scene.camera = cam
cam.rotation_euler = (0, 0, 0)
if FRAMING == "film":
    cam.location = (cx, cy, frame_w * cam_data.lens / 36)
else:
    # Same distance as the stacked film; the sensor sets the visible width.
    cam.location = (REGION_CX, REGION_CY, CAM_DISTANCE)
    cam_data.sensor_fit = "HORIZONTAL"
    cam_data.sensor_width = REGION_W * cam_data.lens / CAM_DISTANCE
    r.resolution_x = round(REGION_W * PX_PER_UNIT)
    r.resolution_y = round(REGION_H * PX_PER_UNIT)


# ------------------------------------------------------------------ lights (soft studio)
def area(name, loc, energy, size, colour="#FFFFFF"):
    ld = bpy.data.lights.new(name, type="AREA")
    ld.energy, ld.size = energy, size
    ld.color = lin(colour)[:3]
    ob = bpy.data.objects.new(name, ld)
    ob.location = (cx + loc[0], cy + loc[1], loc[2])
    tr = ob.constraints.new("TRACK_TO")
    tr.target, tr.track_axis, tr.up_axis = aim, "TRACK_NEGATIVE_Z", "UP_Y"
    scene.collection.objects.link(ob)
    return ob


key_light = area("Key", (-5, 5, 9), 1400, 7)
area("Fill", (6, -3, 8), 450, 9)
area("Rim", (4, 6, 2), 700, 4, BRIGHT_TEAL)
# lighting "reveals" the gradient as the P completes
key(key_light.data, "energy", 0, 1000)
key(key_light.data, "energy", 72, 1000)
key(key_light.data, "energy", 105, 1400)

scene.frame_set(FIRST)
print(f"Payrolla v2 built: layout={LAYOUT} ground={GROUND} framing={FRAMING} "
      f"{r.resolution_x}x{r.resolution_y}.",
      "" if word is None else ("Satoshi loaded." if font else
                               "Satoshi NOT installed: wordmark uses Blender's default font."))
