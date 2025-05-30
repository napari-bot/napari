from abc import ABC, abstractmethod
from typing import Generic, TypeVar, cast

import numpy as np
from vispy.scene import VisualNode
from vispy.visuals.transforms import MatrixTransform

from napari._vispy.utils.gl import BLENDING_MODES, get_max_texture_sizes
from napari.layers import Layer
from napari.utils.events import disconnect_events

_L = TypeVar('_L', bound=Layer)


class VispyBaseLayer(ABC, Generic[_L]):
    """Base object for individual layer views

    Meant to be subclassed.

    Parameters
    ----------
    layer : napari.layers.Layer
        Layer model.
    node : vispy.scene.VisualNode
        Central node with which to interact with the visual.

    Attributes
    ----------
    layer : napari.layers.Layer
        Layer model.
    node : vispy.scene.VisualNode
        Central node with which to interact with the visual.
    scale : sequence of float
        Scale factors for the layer visual in the scenecanvas.
    translate : sequence of float
        Translation values for the layer visual in the scenecanvas.
    MAX_TEXTURE_SIZE_2D : int
        Max texture size allowed by the vispy canvas during 2D rendering.
    MAX_TEXTURE_SIZE_3D : int
        Max texture size allowed by the vispy canvas during 2D rendering.


    Notes
    -----
    _master_transform : vispy.visuals.transforms.MatrixTransform
        Transform positioning the layer visual inside the scenecanvas.
    """

    layer: _L

    def __init__(self, layer: _L, node: VisualNode) -> None:
        super().__init__()
        self.events = None  # Some derived classes have events.

        self.layer = layer
        self._array_like = False
        self.node = node
        self.first_visible = False

        (
            self.MAX_TEXTURE_SIZE_2D,
            self.MAX_TEXTURE_SIZE_3D,
        ) = get_max_texture_sizes()

        self.layer.events.refresh.connect(self._on_refresh_change)
        self.layer.events.set_data.connect(self._on_data_change)
        self.layer.events.visible.connect(self._on_visible_change)
        self.layer.events.opacity.connect(self._on_opacity_change)
        self.layer.events.blending.connect(self._on_blending_change)
        self.layer.events.scale.connect(self._on_matrix_change)
        self.layer.events.translate.connect(self._on_matrix_change)
        self.layer.events.rotate.connect(self._on_matrix_change)
        self.layer.events.shear.connect(self._on_matrix_change)
        self.layer.events.affine.connect(self._on_matrix_change)
        self.layer.experimental_clipping_planes.events.connect(
            self._on_experimental_clipping_planes_change
        )

    @property
    def _master_transform(self):
        """vispy.visuals.transforms.MatrixTransform:
        Central node's firstmost transform.
        """
        # whenever a new parent is set, the transform is reset
        # to a NullTransform so we reset it here
        if not isinstance(self.node.transform, MatrixTransform):
            self.node.transform = MatrixTransform()

        return self.node.transform

    @property
    def translate(self):
        """sequence of float: Translation values."""
        return self._master_transform.matrix[-1, :]

    @property
    def scale(self):
        """sequence of float: Scale factors."""
        matrix = self._master_transform.matrix[:-1, :-1]
        _, upper_tri = np.linalg.qr(matrix)
        return np.diag(upper_tri).copy()

    @property
    def order(self):
        """int: Order in which the visual is drawn in the scenegraph.

        Lower values are closer to the viewer.
        """
        return self.node.order

    @order.setter
    def order(self, order):
        self.node.order = order
        self._on_blending_change()

    @abstractmethod
    def _on_data_change(self):
        raise NotImplementedError

    def _on_refresh_change(self):
        self.node.update()

    def _on_visible_change(self):
        self.node.visible = self.layer.visible

    def _on_opacity_change(self):
        self.node.opacity = self.layer.opacity

    def _on_blending_change(self, event=None):
        blending = self.layer.blending
        blending_kwargs = cast(dict, BLENDING_MODES[blending]).copy()

        if self.first_visible:
            # if the first layer, then we should blend differently
            # the goal is to prevent pathological blending with canvas
            # for minimum, use the src color, ignore alpha & canvas
            if blending == 'minimum':
                src_color_blending = 'one'
                dst_color_blending = 'zero'
            # for additive, use the src alpha and blend to black
            elif blending == 'additive':
                src_color_blending = 'src_alpha'
                dst_color_blending = 'zero'
            # for all others, use translucent blending
            else:
                src_color_blending = 'src_alpha'
                dst_color_blending = 'one_minus_src_alpha'
            blending_kwargs = {
                'depth_test': blending_kwargs['depth_test'],
                'cull_face': False,
                'blend': True,
                'blend_func': (
                    src_color_blending,
                    dst_color_blending,
                    'one',
                    'one',
                ),
                'blend_equation': 'func_add',
            }

        self.node.set_gl_state(**blending_kwargs)
        self.node.update()

    def _on_matrix_change(self):
        dims_displayed = self.layer._slice_input.displayed
        # mypy: self.layer._transforms.simplified cannot be None
        transform = self.layer._transforms.simplified.set_slice(dims_displayed)
        # convert NumPy axis ordering to VisPy axis ordering
        # by reversing the axes order and flipping the linear
        # matrix
        translate = transform.translate[::-1]
        matrix = transform.linear_matrix[::-1, ::-1].T

        # The following accounts for the offset between samples at different
        # resolutions of 3D multi-scale array-like layers (e.g. images).
        # The 2D case is handled differently because that has more complex support
        # (multiple levels, partial field-of-view) that also currently interacts
        # with how pixels are centered (see further below).
        if (
            self._array_like
            and self.layer._slice_input.ndisplay == 3
            and self.layer.multiscale
            and hasattr(self.layer, 'downsample_factors')
        ):
            # The last downsample factor is used because we only ever show the
            # last/lowest multi-scale level for 3D.
            translate += (
                # displayed dimensions, order inverted to match VisPy, then
                # adjust by half a pixel per downscale level
                self.layer.downsample_factors[-1][dims_displayed][::-1] - 1
            ) / 2

        # Embed in the top left corner of a 4x4 affine matrix
        affine_matrix = np.eye(4)
        affine_matrix[: matrix.shape[0], : matrix.shape[1]] = matrix
        affine_matrix[-1, : len(translate)] = translate

        child_offset = np.zeros(len(dims_displayed))

        if self._array_like and self.layer._slice_input.ndisplay == 2:
            # Perform pixel offset to shift origin from top left corner
            # of pixel to center of pixel.
            # Note this offset is only required for array like data in
            # 2D.
            offset_matrix = self.layer._data_to_world.set_slice(
                dims_displayed
            ).linear_matrix
            offset = -offset_matrix @ np.ones(offset_matrix.shape[1]) / 2
            # Convert NumPy axis ordering to VisPy axis ordering
            # and embed in full affine matrix
            affine_offset = np.eye(4)
            affine_offset[-1, : len(offset)] = offset[::-1]
            affine_matrix = affine_matrix @ affine_offset
            if self.layer.multiscale:
                # For performance reasons, when displaying multiscale images,
                # only the part of the data that is visible on the canvas is
                # sent as a texture to the GPU. This means that the texture
                # gets an additional transform, to position the texture
                # correctly offset from the origin of the full data. However,
                # child nodes, which include overlays such as bounding boxes,
                # should *not* receive this offset, so we undo it here:
                child_offset = (
                    np.ones(offset_matrix.shape[1]) / 2
                    - self.layer.corner_pixels[0][dims_displayed][::-1]
                )
            else:
                child_offset = np.full(offset_matrix.shape[1], 1 / 2)
        self._master_transform.matrix = affine_matrix

        child_matrix = np.eye(4)
        child_matrix[-1, : len(child_offset)] = child_offset
        for child in self.node.children:
            child.transform.matrix = child_matrix

    def _on_experimental_clipping_planes_change(self):
        if hasattr(self.node, 'clipping_planes') and hasattr(
            self.layer, 'experimental_clipping_planes'
        ):
            # invert axes because vispy uses xyz but napari zyx
            self.node.clipping_planes = (
                self.layer.experimental_clipping_planes.as_array()[..., ::-1]
            )

    def _on_camera_move(self, event=None):
        return

    def reset(self):
        self._on_visible_change()
        self._on_opacity_change()
        self._on_blending_change()
        self._on_matrix_change()
        self._on_experimental_clipping_planes_change()
        self._on_camera_move()

    def _on_poll(self, event=None):
        """Called when camera moves, before we are drawn.

        Optionally called for some period once the camera stops, so the
        visual can finish up what it was doing, such as loading data into
        VRAM or animating itself.
        """

    def close(self):
        """Vispy visual is closing."""
        disconnect_events(self.layer.events, self)
        self.node.transforms = MatrixTransform()
        self.node.parent = None
