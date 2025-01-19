import bpy
import os
from .utils import (
    show_combiner_formula,
    show_blender_formula,
)

# Imported materials are supposed to perform (highly simplified) high
# level emulation of the N64's RDP pixel shader pipeline.
#
# OVERVIEW OF THE RDP
#
#   ┌───────────┐
#   │ Texture 0 ├──┐
#   └───────────┘  │
#   ┌───────────┐  └───►┌────────────────┐      ┌─────────┐ Output
#   │ Texture 1 ├──────►│ Color Combiner ├─────►│ Blender ├────────►
#   └───────────┘  ┌───►└────────────────┘ ┌───►└─────────┘
#    Colors, etc.  │                       │
#   ───────────────┴───────────────────────┘
#
# COLOR COMBINER
#
# The color combiner is used for effects like combining the texture and
# shading color. It combines four input variables, a, b, c, d, with the
# formula
#
#   Output = (a - b) * c + d
#
# RGB and Alpha are combined separately. In two-cycle mode the color
# combiner runs twice, and the second run can use the output from the
# first run as one of its inputs. Altogether that's 16 inputs in total.
#
#   (4 variables) * (2 RGB/Alpha) * (2 1st/2nd cycle) = 16
#
# The combiner is configured with a 64-bit mux value that specifies the
# source for the each of the 16 inputs.
#
# BLENDER
#
# The blender is used for effects like alpha blending and fog. Similar
# to the combiner, it combines two RGB colors, p and m, with two
# weights, a and b, using the formula
#
#   Output = (p * a + m * b) / (a + b)
#
# Unlike the combiner, it can use the current pixel in the framebuffer
# as input. It, too, can run in two-cycle mode.
#
# TEXTURE WRAPPING
#
# Texture wrapping on the N64 is more complicated than the normal
# "repeat", "clamp", "mirror" modes. Clamping and wrapping can occur
# at configurable edges, NOT just at the boundary of the texture. The
# allows eg. a texture to repeat only a fixed number of times.
#
# The manual gives this example, which first clamps, then wraps with
# mirroring.
#
#  0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,...    Input Coordinate
#  0,1,2,3,3,2,1,0,0,1, 2, 3, 3, 3, 3, 3,...    Wrapped Coordinates
#         ▲                 ▲
#         │                 └── clamp edge
#         └──────────────────── wrap edge
#
# REFERENCES
#
# http://n64devkit.square7.ch/tutorial/graphics/
# http://n64devkit.square7.ch/pro-man/pro12/index.htm
# https://hack64.net/wiki/doku.php?id=rcpstructs
# Angrylion's RDP Plus


def setup_n64_material(material, texture_dir, *args):
    return N64Shader(material, texture_dir).setup(*args)


class N64Shader:
    def __init__(self, material, texture_dir):
        self.material = material
        self.texture_dir = texture_dir

        material.use_nodes = True
        self.node_tree = material.node_tree
        self.nodes = material.node_tree.nodes
        self.links = material.node_tree.links

        self.use_alpha = False
        self.vars = {'0': 0, '1': 1}

    def setup(
        self,
        combiner1, combiner2,
        blender1, blender2,
        tex0, tex1,
        cull_backfacing,
        is_translucent,
        show_alpha,
    ):
        mat = self.material

        self.nodes.clear()

        # Gather all input sources the shader needs
        sources = []
        sources += combiner1
        sources += combiner2 or []
        sources += blender1
        sources += blender2 or []

        self.make_inputs(tex0, tex1, sources)
        self.make_combiners(combiner1, combiner2)
        self.make_blenders(blender1, blender2)
        self.make_output()

        # Set material-level properties
        mat.use_backface_culling = cull_backfacing
        if self.use_alpha and show_alpha:
            mat.blend_method = 'BLEND' if is_translucent else 'HASHED'
        else:
            mat.blend_method = 'OPAQUE'

        # Custom props (useful for debugging)
        mat['N64 Texture 0'] = show_texture_info(tex0) if tex0['crc'] else ''
        mat['N64 Texture 1'] = show_texture_info(tex1) if tex1['crc'] else ''
        mat['N64 Color Combiner 1'] = show_combiner_formula(*combiner1[:4])
        mat['N64 Alpha Combiner 1'] = show_combiner_formula(*combiner1[4:])
        mat['N64 Color Combiner 2'] = show_combiner_formula(*combiner2[:4]) if combiner2 else ''
        mat['N64 Alpha Combiner 2'] = show_combiner_formula(*combiner2[4:]) if combiner2 else ''
        mat['N64 Blender 1'] = show_blender_formula(*blender1)
        mat['N64 Blender 2'] = show_blender_formula(*blender2) if blender2 else ''

    def connect(self, v, socket):
        """
        Connect a socket to an input source.

        The input source can be another socket, a constant value, or a
        named variable to take from self.vars.
        """
        if isinstance(v, str):
            v = self.vars[v]

        if isinstance(v, bpy.types.NodeSocket):
            self.links.new(v, socket)
        else:
            if isinstance(v, (int, float)) and socket.type == 'RGBA':
                v = (v, v, v, 1.0)
            socket.default_value = v

    def new_color_math_node(self, blend_type):
        """
        Creates a node for color math.

        Returns the node, the two input sockets, and the output socket.
        """
        node = self.nodes.new('ShaderNodeMix')
        node.data_type = 'RGBA'
        node.blend_type = blend_type
        node.inputs[0].default_value = 1  # Fac
        return node, node.inputs[6], node.inputs[7], node.outputs[2]

    def get_next_x_position(self):
        """
        Get the X position to put the next block of nodes at.

        Blocks are created from left to right following the path of
        the "Combined Color" and "Combined Alpha" variables. Gets a
        point to the right of those sockets.
        """
        # Start position
        locs = [-630]

        for arg in ['Combined Color', 'Combined Alpha']:
            if arg not in self.vars:
                continue
            if not isinstance(self.vars[arg], bpy.types.NodeSocket):
                continue
            x = self.vars[arg].node.location[0]
            x += 300  # move node width + gutter
            locs.append(x)

        return max(locs)

    def make_combiners(self, combiner1, combiner2):
        x = self.get_next_x_position()
        self.make_color_combiner(combiner1[:4], location=(x, 500))
        self.make_alpha_combiner(combiner1[4:], location=(x, 0))

        if not combiner2:
            return

        x = self.get_next_x_position()
        self.make_color_combiner(combiner2[:4], location=(x, 500))
        self.make_alpha_combiner(combiner2[4:], location=(x, 0))

    def make_blenders(self, blender1, blender2):
        x = self.get_next_x_position()
        y = 220

        for blender in [blender1, blender2]:
            if not blender:
                break

            node = self.make_simple_blender_mix_node(blender)
            if node:
                node.location = x, y
                x += 320

            # The only blend mode we can do in Blender is alpha
            # blending, so if the blender reads from the framebuffer
            # *at all*, we crudely assume it is doing alpha blending.
            if 'Framebuffer Color' in blender:
                if self.vars['Combined Alpha'] != 1:
                    self.use_alpha = True

                # Since alpha blending occurs after the shader, we
                # can't do anything else, so stop here.
                break

    def make_output(self):
        x = self.get_next_x_position()

        # If the shader needs alpha blending, combine the color and
        # alpha with a Transparent BSDF + Mix Shader.
        if self.use_alpha:
            node_mix = self.nodes.new('ShaderNodeMixShader')
            node_trans = self.nodes.new('ShaderNodeBsdfTransparent')

            node_mix.location = x + 200, 300
            node_trans.location = x, 400
            x += 500

            self.connect('Combined Alpha', node_mix.inputs[0])
            self.connect(node_trans.outputs[0], node_mix.inputs[1])
            self.connect('Combined Color', node_mix.inputs[2])

        node_out = self.nodes.new('ShaderNodeOutputMaterial')
        node_out.location = x, 160
        if self.use_alpha:
            self.connect(node_mix.outputs[0], node_out.inputs[0])
        else:
            self.connect('Combined Color', node_out.inputs[0])

    def make_color_combiner(self, combiner, location):
        a, b, c, d = combiner
        x, y = location

        # Early out for (a-b)*0 + d = d
        if c == '0':
            if isinstance(self.vars[d], bpy.types.NodeSocket):
                self.vars['Combined Color'] = self.vars[d]
            else:
                # Slightly awkward case. self.connect connects scalars
                # to a Color socket by putting them in the socket's
                # default_value. But because "Combined Color" may get
                # connected to a Shader socket, which has no
                # default_value, it needs to be a real socket, not a
                # scalar. So in this case we create an RGB node to
                # supply the constant value.
                node = self.nodes.new('ShaderNodeRGB')
                node.location = x, y
                self.connect(self.vars[d], node.outputs[0])
                self.vars['Combined Color'] = node.outputs[0]
            return

        frame = self.nodes.new('NodeFrame')
        frame.label = show_combiner_formula(a, b, c, d)

        # A - B
        node, in1, in2, out1 = self.new_color_math_node('SUBTRACT')
        node.location = x, y
        node.parent = frame
        self.connect(a, in1)
        self.connect(b, in2)

        # * C
        node, in1, in2, out2 = self.new_color_math_node('MULTIPLY')
        node.location = x + 230, y - 120
        node.parent = frame
        self.connect(out1, in1)
        self.connect(c, in2)

        # + D
        node, in1, in2, out3 = self.new_color_math_node('ADD')
        node.location = x + 460, y - 240
        node.parent = frame
        self.connect(out2, in1)
        self.connect(d, in2)

        self.vars['Combined Color'] = out3

    def make_alpha_combiner(self, combiner, location):
        a, b, c, d = combiner
        x, y = location

        # Early out for (a-b)*0 + d = d
        if c == '0':
            self.vars['Combined Alpha'] = self.vars[d]
            return

        frame = self.nodes.new('NodeFrame')
        frame.label = show_combiner_formula(a, b, c, d)

        # A - B
        node1 = self.nodes.new('ShaderNodeMath')
        node1.operation = 'SUBTRACT'
        node1.location = x + 90, y - 130
        node1.parent = frame
        self.connect(a, node1.inputs[0])
        self.connect(b, node1.inputs[1])

        # * C + D
        node2 = self.nodes.new('ShaderNodeMath')
        node2.operation = 'MULTIPLY_ADD'
        node2.location = x + 370, y - 150
        node2.parent = frame
        self.connect(node1.outputs[0], node2.inputs[0])
        self.connect(c, node2.inputs[1])
        self.connect(d, node2.inputs[2])

        self.vars['Combined Alpha'] = node2.outputs[0]

    def make_simple_blender_mix_node(self, blender):
        """
        Creates a mix node when the blender does simple mixing.

        In this case the blender basically functions as another color
        combiner. Fogging is an important special case.

        Returns the node if created, or None if the blender wasn't
        simple enough.
        """
        p, a, m, b = blender

        # It's simple if...
        is_simple = (
            # b = 1 - a, so that
            # (p*a + m*(1-a)) / (a + (1-a)) = mix(m, p, a)
            b == 'One Minus A' and
            # Reading from framebuffer is not required
            p not in ['Framebuffer Color', 'Framebuffer Alpha'] and
            m not in ['Framebuffer Color', 'Framebuffer Alpha']
        )
        if not is_simple:
            return None

        node, in1, in2, out = self.new_color_math_node('MIX')
        self.connect(a, node.inputs[0])
        self.connect(m, in1)
        self.connect(p, in2)
        self.vars['Combined Color'] = out

        if a == 'Fog Level':
            frame = self.nodes.new('NodeFrame')
            frame.label = 'Fog'
            node.parent = frame

        return node

    def make_inputs(self, tex0, tex1, input_vars):
        x, y = -1100, 500

        for var in input_vars:
            # Already created?
            if var in self.vars:
                continue

            # Texture inputs
            for i in range(2):
                if var in [f'Texel {i} Color', f'Texel {i} Alpha']:
                    tex = tex0 if i == 0 else tex1
                    node = self.make_texture_unit(tex, i, location=(x, y))
                    y -= 400
                    self.vars[f'Texel {i} Color'] = node.outputs['Color']
                    self.vars[f'Texel {i} Alpha'] = node.outputs['Alpha']

            # Vertex Color inputs
            for vc in ['Shade', 'Primitive', 'Env', 'Blend', 'Fog']:
                if var in [f'{vc} Color', f'{vc} Alpha']:
                    node = self.nodes.new('ShaderNodeVertexColor')
                    node.location = x, y
                    y -= 200
                    node.layer_name = f'{vc} Color'
                    node.name = node.label = f'{vc} Color'
                    self.vars[f'{vc} Color'] = node.outputs['Color']
                    self.vars[f'{vc} Alpha'] = node.outputs['Alpha']

            # Scalar attributes
            if var in ['Fog Level', 'Primitive LOD Fraction']:
                node = self.nodes.new('ShaderNodeAttribute')
                node.location = x, y
                y -= 290
                if var == 'Fog Level':
                    node.attribute_name = 'Fog Level'
                elif var == 'Primitive LOD Fraction':
                    node.attribute_name = 'Primitive LOD'
                node.name = node.label = var
                self.vars[var] = node.outputs['Fac']

            # Use constant 0 for LOD fraction. Usually LOD is used for
            # mipmapping, and 0 "should" pick the highest detail
            # level. But certain effects (like Peach's portrait
            # morphing into Bowser's in SM64) won't work.
            if var == 'LOD Fraction':
                node = self.nodes.new('ShaderNodeValue')
                node.location = x, y
                y -= 200
                node.outputs[0].default_value = 0.0
                node.label = var
                self.vars[var] = node.outputs[0]

            # Not yet implemented
            unimplemented = [
                'Key Center',
                'Key Scale',
                'Noise',
                'Convert K4',
                'Convert K5',
            ]
            for un_var in unimplemented:
                if var == un_var:
                    print('GLR Import: unimplemented color combiner input:', un_var)
                    node = self.nodes.new('ShaderNodeRGB')
                    node.location = x, y
                    y -= 300
                    node.outputs[0].default_value = (0.0, 1.0, 1.0, 1.0)
                    node.label = f'{un_var} (UNIMPLEMENTED)'
                    self.vars[un_var] = node.outputs[0]

    def load_image(self, crc):
        if crc == 0:
            return None

        filepath = os.path.join(self.texture_dir, f'{crc:016X}.png')
        try:
            image = bpy.data.images.load(filepath, check_existing=True)
        except Exception:
            # Image didn't exist
            # Allow the path to be resolved later
            image = bpy.data.images.new(os.path.basename(filepath), 16, 16)
            image.filepath = filepath
            image.source = 'FILE'

        return image

    def make_texture_unit(self, tex, tex_num, location):
        x, y = location

        # Image Texture node
        node_tex = self.nodes.new('ShaderNodeTexImage')
        node_tex.name = node_tex.label = f'Texture {tex_num}'
        node_tex.width = 290
        node_tex.location = x - 150, y
        node_tex.image = self.load_image(tex['crc'])
        node_tex.interpolation = tex['filter']
        uv_socket = node_tex.inputs[0]

        x -= 370

        # Wrapping
        uv_socket = self.make_texcoord_wrapper(tex, node_tex, location=(x, y))
        x, y = uv_socket.node.location

        x -= 220

        # UVMap node
        node_uv = self.nodes.new('ShaderNodeUVMap')
        node_uv.name = node_uv.label = f'UV Map Texture {tex_num}'
        node_uv.location = x - 160, y
        node_uv.uv_map = tex['uv_map']
        self.connect(node_uv.outputs[0], uv_socket)

        return node_tex

    def make_texcoord_wrapper(self, tex, node_tex, location):
        # NOTE: Kirby 64's title screen is a good test for texture
        # wrapping.

        # First we check if the clamp/wrap boundaries line up with the
        # edge of the texture. If they do they can be done with a
        # classic GL_REPEAT-type texture mode. If both directions are
        # the same, we can do the whole wrapping calculation with the
        # Image Texture node's extension property.

        extensions = [None, None]
        for i in [0, 1]:
            clamp = tex['clampS'] if i == 0 else tex['clampT']
            wrap = tex['wrapS'] if i == 0 else tex['wrapT']
            mirror = tex['mirrorS'] if i == 0 else tex['mirrorT']

            # Clamps at image edge, no wrap
            if clamp == 1 and (wrap == 0 or wrap >= 1):
                extensions[i] = 'EXTEND'
            # No clamp, wraps at texture edge
            elif clamp == 0 and wrap == 1:
                extensions[i] = 'MIRROR' if mirror else 'REPEAT'
            # No clamp, no wrap (TODO: confirm this)
            elif clamp == 0 and wrap == 0:
                extensions[i] = 'EXTEND'

        if extensions[0] == extensions[1] and extensions[0] != None:
            node_tex.extension = extensions[i]
            return node_tex.inputs[0]

        # Otherwise, separate the U and V and do clamp-wrap-mirror
        # using math nodes.

        node_tex.extension = 'EXTEND'

        frame = self.nodes.new('NodeFrame')
        frame.label = 'Clamp Wrap Mirror Texcoord'

        x, y = location

        # Combine XYZ
        node_com = self.nodes.new('ShaderNodeCombineXYZ')
        node_com.parent = frame
        node_com.location = x - 80, y - 110
        self.connect(node_com.outputs[0], node_tex.inputs[0])

        # Separate XYZ
        node_sep = self.nodes.new('ShaderNodeSeparateXYZ')
        node_sep.parent = frame
        node_sep.location = x - 80, y - 110

        for i in [0, 1]:
            clamp = tex['clampS'] if i == 0 else tex['clampT']
            wrap = tex['wrapS'] if i == 0 else tex['wrapT']
            mirror = tex['mirrorS'] if i == 0 else tex['mirrorT']

            socket = node_com.inputs[i]

            x, y = location
            x -= 120
            y -= 200 * i

            # The clamp/wrap edges are given for a V >= 0 space. But
            # we do (u,1-v) when importing UVs (because textures are
            # upside down?), which turns it into a V <= 1 space.
            #
            # Converting the Ping Pong node for this space seems
            # annoying, so instead, for the V direction only, we
            # convert back to V >= 0 space with a 1-x Math node, do
            # the wrapping as normal, then convert back again.
            #
            # This is rather ugly :/
            if i == 1:
                node = self.nodes.new('ShaderNodeMath')
                node.parent = frame
                node.location = x - 140, y
                node.operation = 'SUBTRACT'
                self.connect(node.outputs[0], socket)
                node.inputs[0].default_value = 1
                socket = node.inputs[1]
                x -= 200

            if wrap > 0:
                if mirror:
                    # Mirror with a Math/Ping Pong node
                    node = self.nodes.new('ShaderNodeMath')
                    node.parent = frame
                    node.location = x - 140, y
                    node.operation = 'PINGPONG'
                    self.connect(node.outputs[0], socket)
                    socket = node.inputs[0]
                    node.inputs[1].default_value = wrap  # scale
                else:
                    # Wrap with a Math/Wrap node
                    node = self.nodes.new('ShaderNodeMath')
                    node.parent = frame
                    node.location = x - 140, y
                    node.operation = 'WRAP'
                    self.connect(node.outputs[0], socket)
                    socket = node.inputs[0]
                    node.inputs[1].default_value = 0     # min
                    node.inputs[2].default_value = wrap  # max
                x -= 200

            if clamp > 0:
                # Clamp
                node = self.nodes.new('ShaderNodeClamp')
                node.parent = frame
                node.location = x - 140, y
                self.connect(node.outputs[0], socket)
                socket = node.inputs[0]
                node.inputs[1].default_value = 0      # min
                node.inputs[2].default_value = clamp  # max
                x -= 200

            # 1 - V converts V back into original UV space
            if i == 1:
                node = self.nodes.new('ShaderNodeMath')
                node.parent = frame
                node.location = x - 140, y
                node.operation = 'SUBTRACT'
                self.connect(node.outputs[0], socket)
                node.inputs[0].default_value = 1
                socket = node.inputs[1]
                x -= 200

            self.connect(node_sep.outputs[i], socket)

            node_sep.location[0] = min(node_sep.location[0], x - 200)

        return node_sep.inputs[0]


def show_texture_info(tex):
    crc = tex['crc']
    tfilter = tex['filter']
    return f'{crc:016X},{tfilter}'
