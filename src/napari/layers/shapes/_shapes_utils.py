from __future__ import annotations

import itertools
import tempfile
import typing
from typing import TYPE_CHECKING, overload

import numpy as np
from skimage import measure
from skimage.draw import line, polygon2mask
from vispy.geometry import Triangulation
from vispy.visuals.tube import _frenet_frames

from napari.layers.shapes import (
    _accelerated_triangulate_dispatch as _triangulate_dispatch,
    _accelerated_triangulate_python as _triangulate_py,
)
from napari.layers.shapes.shape_types import (
    BoxArray,
    CoordinateArray,
    CoordinateArray2D,
    CoordinateArray3D,
    EdgeArray,
    TriangleArray,
)
from napari.utils.translations import trans

if TYPE_CHECKING:
    import numpy.typing as npt

try:
    # see https://github.com/vispy/vispy/issues/1029
    from triangle import triangulate
except ModuleNotFoundError:
    triangulate = None


def find_planar_axis(
    points: CoordinateArray,
) -> tuple[CoordinateArray2D, int | None, float | None]:
    """Find an axis along which the input points are planar.

    If points are 2D, they are returned unchanged.

    If there is a planar axis, return the corresponding 2D points, the axis
    position, and the coordinate of the plane.

    If there is *no* planar axis, return an empty dataset.

    Parameters
    ----------
    points : array, shape (npoints, ndim)
        An array of point coordinates. ``ndim`` must be 2 or 3.

    Returns
    -------
    points2d : array, shape (npoints | 0, 2)
        Array of 2D points. May be empty if input points are not planar along
        any axis.
    axis_idx : int | None
        The axis along which points are planar.
    value : float | None
        The coordinate of the points along ``axis``.
    """
    ndim = points.shape[1]
    if ndim == 2:
        return points, None, None  # type: ignore[return-value]
    for axis_idx in range(ndim):
        values = np.unique(points[:, axis_idx])
        if len(values) == 1:
            return np.delete(points, axis_idx, axis=1), axis_idx, values[0]
    return np.empty((0, 2), dtype=points.dtype), None, None  # type: ignore[return-value]


@typing.overload
def _fan_triangulation(
    poly: CoordinateArray2D,
) -> tuple[CoordinateArray2D, TriangleArray]: ...


@typing.overload
def _fan_triangulation(
    poly: CoordinateArray3D,
) -> tuple[CoordinateArray3D, TriangleArray]: ...


def _fan_triangulation(
    poly: CoordinateArray,
) -> tuple[CoordinateArray, TriangleArray]:
    """Return a fan triangulation of a given polygon.

    https://en.wikipedia.org/wiki/Fan_triangulation

    Parameters
    ----------
    poly: numpy array of float, shape (N, 3) or (N, 2)
        Polygon vertices, in order.

    Returns
    -------
    vertices : numpy array of float, shape (N, 3) or (N, 2)
        The vertices of the triangulation. In this case, the input array.
    triangles : numpy array of int, shape (N, 3)
        The triangles of the triangulation, as triplets of indices into the
        vertices array.
    """
    vertices = np.copy(poly)
    triangles = np.zeros((len(poly) - 2, 3), dtype=np.uint32)
    triangles[:, 1] = np.arange(1, len(poly) - 1)
    triangles[:, 2] = np.arange(2, len(poly))
    return vertices, triangles  # type: ignore[return-value]


def inside_boxes(boxes):
    """Checks which boxes contain the origin. Boxes need not be axis aligned

    Parameters
    ----------
    boxes : (N, 8, 2) array
        Array of N boxes that should be checked

    Returns
    -------
    inside : (N,) array of bool
        True if corresponding box contains the origin.
    """

    AB = boxes[:, 0] - boxes[:, 6]
    AM = boxes[:, 0]
    BC = boxes[:, 6] - boxes[:, 4]
    BM = boxes[:, 6]

    ABAM = np.multiply(AB, AM).sum(1)
    ABAB = np.multiply(AB, AB).sum(1)
    BCBM = np.multiply(BC, BM).sum(1)
    BCBC = np.multiply(BC, BC).sum(1)

    c1 = ABAM >= 0
    c2 = ABAM <= ABAB
    c3 = BCBM >= 0
    c4 = BCBM <= BCBC

    inside = np.all(np.array([c1, c2, c3, c4]), axis=0)

    return inside


def triangles_intersect_box(triangles, corners):
    """Determines which triangles intersect an axis aligned box.

    Parameters
    ----------
    triangles : (N, 3, 2) array
        Array of vertices of triangles to be tested
    corners : (2, 2) array
        Array specifying corners of a box

    Returns
    -------
    intersects : (N,) array of bool
        Array with `True` values for triangles intersecting the box
    """

    vertices_inside = triangle_vertices_inside_box(triangles, corners)
    edge_intersects = triangle_edges_intersect_box(triangles, corners)

    intersects = np.logical_or(vertices_inside, edge_intersects)

    return intersects


def triangle_vertices_inside_box(triangles, corners):
    """Determines which triangles have vertices inside an axis aligned box.

    Parameters
    ----------
    triangles : (N, 3, 2) array
        Array of vertices of triangles to be tested
    corners : (2, 2) array
        Array specifying corners of a box

    Returns
    -------
    inside : (N,) array of bool
        Array with `True` values for triangles with vertices inside the box
    """
    box = create_box(corners)[[0, 4]]

    vertices_inside = np.empty(triangles.shape[:-1], dtype=bool)
    for i in range(3):
        # check if each triangle vertex is inside the box
        below_top = np.all(box[1] >= triangles[:, i, :], axis=1)
        above_bottom = np.all(triangles[:, i, :] >= box[0], axis=1)
        vertices_inside[:, i] = np.logical_and(below_top, above_bottom)

    inside = np.any(vertices_inside, axis=1)

    return inside


def triangle_edges_intersect_box(triangles, corners):
    """Determines which triangles have edges that intersect the edges of an
    axis aligned box.

    Parameters
    ----------
    triangles : (N, 3, 2) array
        Array of vertices of triangles to be tested
    corners : (2, 2) array
        Array specifying corners of a box

    Returns
    -------
    intersects : (N,) array of bool
        Array with `True` values for triangles with edges that intersect the
        edges of the box.
    """
    box = create_box(corners)[[0, 2, 4, 6]]

    intersects = np.zeros([len(triangles), 12], dtype=bool)
    for i in range(3):
        # check if each triangle edge
        p1 = triangles[:, i, :]
        q1 = triangles[:, (i + 1) % 3, :]

        for j in range(4):
            # Check the four edges of the box
            p2 = box[j]
            q2 = box[(j + 1) % 3]
            intersects[:, i * 3 + j] = [
                lines_intersect(p1[k], q1[k], p2, q2) for k in range(len(p1))
            ]

    return np.any(intersects, axis=1)


def lines_intersect(p1, q1, p2, q2):
    """Determines if line segment p1q1 intersects line segment p2q2

    Parameters
    ----------
    p1 : (2,) array
        Array of first point of first line segment
    q1 : (2,) array
        Array of second point of first line segment
    p2 : (2,) array
        Array of first point of second line segment
    q2 : (2,) array
        Array of second point of second line segment

    Returns
    -------
    intersects : bool
        Bool indicating if line segment p1q1 intersects line segment p2q2
    """
    # Determine four orientations
    o1 = _triangulate_py.orientation(p1, q1, p2)
    o2 = _triangulate_py.orientation(p1, q1, q2)
    o3 = _triangulate_py.orientation(p2, q2, p1)
    o4 = _triangulate_py.orientation(p2, q2, q1)

    # Test general case
    if (o1 != o2) and (o3 != o4):
        return True

    # Test special cases
    # p1, q1 and p2 are collinear and p2 lies on segment p1q1
    if o1 == 0 and on_segment(p1, p2, q1):
        return True

    # p1, q1 and q2 are collinear and q2 lies on segment p1q1
    if o2 == 0 and on_segment(p1, q2, q1):
        return True

    # p2, q2 and p1 are collinear and p1 lies on segment p2q2
    if o3 == 0 and on_segment(p2, p1, q2):
        return True

    # p2, q2 and q1 are collinear and q1 lies on segment p2q2
    if o4 == 0 and on_segment(p2, q1, q2):  # noqa: SIM103
        return True

    # Doesn't fall into any special cases
    return False


def on_segment(p, q, r):
    """Checks if q is on the segment from p to r

    Parameters
    ----------
    p : (2,) array
        Array of first point of segment
    q : (2,) array
        Array of point to check if on segment
    r : (2,) array
        Array of second point of segment

    Returns
    -------
    on : bool
        Bool indicating if q is on segment from p to r
    """
    if max(p[0], r[0]) >= q[0] >= min(p[0], r[0]) and max(p[1], r[1]) >= q[
        1
    ] >= min(p[1], r[1]):
        on = True
    else:
        on = False

    return on


def is_collinear(points: npt.NDArray) -> bool:
    """Determines if a list of 2D points are collinear.

    Parameters
    ----------
    points : (N, 2) array
        Points to be tested for collinearity

    Returns
    -------
    val : bool
        True is all points are collinear, False otherwise.
    """
    if len(points) < 3:
        return True

    # The collinearity test takes three points, the first two are the first
    # two in the list, and then the third is iterated through in the loop
    return all(
        _triangulate_py.orientation(points[0], points[1], p) == 0
        for p in points[2:]
    )


def point_to_lines(point, lines):
    """Calculate the distance between a point and line segments and returns the
    index of the closest line. First calculates the distance to the infinite
    line, then checks if the projected point lies between the line segment
    endpoints. If not, calculates distance to the endpoints

    Parameters
    ----------
    point : np.ndarray
        1x2 array of specifying the point
    lines : np.ndarray
        Nx2x2 array of line segments

    Returns
    -------
    index : int
        Integer index of the closest line
    location : float
        Normalized location of intersection of the distance normal to the line
        closest. Less than 0 means an intersection before the line segment
        starts. Between 0 and 1 means an intersection inside the line segment.
        Greater than 1 means an intersection after the line segment ends
    """

    # shift and normalize vectors
    lines_vectors = lines[:, 1] - lines[:, 0]
    point_vectors = point - lines[:, 0]
    end_point_vectors = point - lines[:, 1]
    norm_lines = np.linalg.norm(lines_vectors, axis=1, keepdims=True)
    reject = (norm_lines == 0).squeeze()
    norm_lines[reject] = 1
    unit_lines = lines_vectors / norm_lines

    # calculate distance to line (2D cross-product)
    line_dist = abs(
        unit_lines[..., 0] * point_vectors[..., 1]
        - unit_lines[..., 1] * point_vectors[..., 0]
    )

    # calculate scale
    line_loc = (unit_lines * point_vectors).sum(axis=1) / norm_lines.squeeze()

    # for points not falling inside segment calculate distance to appropriate
    # endpoint
    line_dist[line_loc < 0] = np.linalg.norm(
        point_vectors[line_loc < 0], axis=1
    )
    line_dist[line_loc > 1] = np.linalg.norm(
        end_point_vectors[line_loc > 1], axis=1
    )
    line_dist[reject] = np.linalg.norm(point_vectors[reject], axis=1)
    line_loc[reject] = 0.5

    # calculate closet line
    index = np.argmin(line_dist)
    location = line_loc[index]

    return index, location


def create_box(data: CoordinateArray2D) -> BoxArray:
    """Creates the axis aligned interaction box of a list of points

    Parameters
    ----------
    data : np.ndarray
        Nx2 array of points whose interaction box is to be found

    Returns
    -------
    box : np.ndarray
        9x2 array of vertices of the interaction box. The first 8 points are
        the corners and midpoints of the box in clockwise order starting in the
        upper-left corner. The last point is the center of the box
    """
    min_val = [data[:, 0].min(axis=0), data[:, 1].min(axis=0)]
    max_val = [data[:, 0].max(axis=0), data[:, 1].max(axis=0)]
    tl = np.array([min_val[0], min_val[1]])
    tr = np.array([max_val[0], min_val[1]])
    br = np.array([max_val[0], max_val[1]])
    bl = np.array([min_val[0], max_val[1]])
    box = np.array(
        [
            tl,
            (tl + tr) / 2,
            tr,
            (tr + br) / 2,
            br,
            (br + bl) / 2,
            bl,
            (bl + tl) / 2,
            (tl + tr + br + bl) / 4,
        ]
    )
    return box


def rectangle_to_box(
    data: np.ndarray[tuple[typing.Literal[4], int], np.dtype[np.float32]],
) -> BoxArray:
    """Converts the four corners of a rectangle into a interaction box like
    representation. If the rectangle is not axis aligned the resulting box
    representation will not be axis aligned either

    Parameters
    ----------
    data : np.ndarray
        4xD array of corner points to be converted to a box like representation

    Returns
    -------
    box : np.ndarray
        9xD array of vertices of the interaction box. The first 8 points are
        the corners and midpoints of the box in clockwise order starting in the
        upper-left corner. The last point is the center of the box
    """
    if data.shape[0] != 4:
        raise ValueError(
            trans._(
                'Data shape does not match expected `[4, D]` shape specifying corners for the rectangle',
                deferred=True,
            )
        )
    box = np.array(
        [
            data[0],
            (data[0] + data[1]) / 2,
            data[1],
            (data[1] + data[2]) / 2,
            data[2],
            (data[2] + data[3]) / 2,
            data[3],
            (data[3] + data[0]) / 2,
            data.mean(axis=0),
        ]
    )
    return box


def find_corners(data: npt.NDArray) -> npt.NDArray:
    """Finds the four corners of the interaction box defined by an array of
    points

    Parameters
    ----------
    data : np.ndarray
        Nx2 array of points whose interaction box is to be found

    Returns
    -------
    corners : np.ndarray
        4x2 array of corners of the bounding box
    """
    min_val = data.min(axis=0)
    max_val = data.max(axis=0)
    tl = np.array([min_val[0], min_val[1]])
    tr = np.array([max_val[0], min_val[1]])
    br = np.array([max_val[0], max_val[1]])
    bl = np.array([min_val[0], max_val[1]])
    corners = np.array([tl, tr, br, bl])
    return corners


def center_radii_to_corners(
    center: npt.NDArray, radii: npt.NDArray
) -> npt.NDArray:
    """Expands a center and radii into a four corner rectangle

    Parameters
    ----------
    center : np.ndarray
        Length 2 array of the center coordinates.
    radii : np.ndarray
        Length 2 array of the two radii.

    Returns
    -------
    corners : np.ndarray
        4x2 array of corners of the bounding box.
    """
    data = np.array([center + radii, center - radii])
    corners = find_corners(data)
    return corners


def triangulate_ellipse(
    corners: CoordinateArray, num_segments: int = 100
) -> tuple[CoordinateArray, TriangleArray]:
    """Determines the triangulation of a path. The resulting `offsets` can
    multiplied by a `width` scalar and be added to the resulting `centers`
    to generate the vertices of the triangles for the triangulation, i.e.
    `vertices = centers + width*offsets`. Using the `centers` and `offsets`
    representation thus allows for the computed triangulation to be
    independent of the line width.

    Parameters
    ----------
    corners : np.ndarray
        4xD array of four bounding corners of the ellipse. The ellipse will
        still be computed properly even if the rectangle determined by the
        corners is not axis aligned. D in {2,3}
    num_segments : int
        Integer determining the number of segments to use when triangulating
        the ellipse

    Returns
    -------
    vertices : np.ndarray
        Mx2/Mx3 array coordinates of vertices for triangulating an ellipse.
        Includes the center vertex of the ellipse, followed by `num_segments`
        vertices around the boundary of the ellipse (M = `num_segments`+1)
    triangles : np.ndarray
        Px3 array of the indices of the vertices for the triangles of the
        triangulation. Has length (P) given by `num_segments`,
        (P = M-1 = num_segments)

    Notes
    -----
    Despite it's name the ellipse will have num_segments-1 segments on their outline.
    That is to say num_segments=7 will lead to ellipses looking like hexagons.

    The behavior of this function is not well defined if the ellipse is degenerate
    in the current plane/volume you are currently observing.


    """
    if corners.shape[0] != 4:
        raise ValueError(
            trans._(
                'Data shape does not match expected `[4, D]` shape specifying corners for the ellipse',
                deferred=True,
            )
        )
    assert corners.shape in {(4, 2), (4, 3)}
    center = corners.mean(axis=0)
    adjusted = corners - center

    # Take to consecutive corners difference
    # that give us the 1/2 minor and major axes.
    ax1 = (adjusted[1] - adjusted[0]) / 2
    ax2 = (adjusted[2] - adjusted[1]) / 2
    # Compute the transformation matrix from the unit circle
    # to our current ellipse.
    # ... it's easy just the 1/2 minor/major axes for the two column
    # note that our transform shape will depends on whether we are 2D-> 2D (matrix, 2 by 2),
    # or 2D -> 3D (matrix 2 by 3).
    transform = np.stack((ax1, ax2))
    if corners.shape == (4, 2):
        assert transform.shape == (2, 2)
    else:
        assert transform.shape == (2, 3)

    # we discretize the unit circle always in 2D.
    v2d = np.zeros((num_segments + 1, 2), dtype=np.float32)
    theta = np.linspace(0, np.deg2rad(360), num_segments)
    v2d[1:, 0] = np.cos(theta)
    v2d[1:, 1] = np.sin(theta)

    # ! vertices shape can be 2,M or 3,M depending on the transform.
    vertices = np.matmul(v2d, transform)

    # Shift back to center
    vertices = vertices + center

    triangles = (
        np.arange(num_segments) + np.array([[0], [1], [2]])
    ).T * np.array([0, 1, 1])
    triangles[-1, 2] = 1

    return vertices, triangles


def _cull_triangles_not_in_poly(vertices, triangles, poly):
    """Remove triangles that are not inside the polygon.

    Unlike VisPy, triangle's constrained Delaunay triangulation
    returns triangles inside the hole as well. (I guess in case you want
    to render holes but in a different color, for example.) In our case,
    we want to get rid of them, so we cull them with some NumPy
    calculations.

    Parameters
    ----------
    vertices: np.ndarray[np.floating], shape (N, 2)
        The vertices of the triangulation.
    triangles: np.ndarray[np.intp], shape (M, 3)
        Triangles in the triangulation, defined by three indices into the
        vertex array.
    poly: np.ndarray[np.floating], shape (P, 2)
        The vertices of the polygon, in order. Holes in the polygon are
        defined by an embedded polygon that starts from an arbitrary point in
        the enclosing polygon and wind in the opposite direction.

    Returns
    -------
    culled_triangles: np.ndarray[np.intp], shape (P, 3), P ≤ M
        A subset of the input triangles.
    """
    centers = np.mean(vertices[triangles], axis=1)
    in_poly = measure.points_in_poly(centers, poly)
    return triangles[in_poly]


def _fix_vertices_if_needed(
    vertices: CoordinateArray2D, axis: int | None, value: float | None
) -> CoordinateArray:
    """Ensure vertices are planar along a given axis.

    If an axis and value are provided, this function inserts the value as
    a new coordinate along the given axis.
    Used to convert 2D vertices in 3D vertices along the plane.

    Parameters
    ----------
    vertices: np.ndarray[np.floating], shape (N, 2)
        The vertices of the triangulation.
    axis: int
        The axis along which the vertices are planar.
    value: float
        The coordinate of the plane.

    Returns
    -------
    new_vertices: np.ndarray[np.floating], shape (N, 3)
        The vertices of the triangulation with the given axis fixed.
    """
    if axis is None or value is None:
        return vertices
    new_vertices = np.insert(vertices, axis, value, axis=1)
    return new_vertices


def triangulate_face_and_edges(
    polygon_vertices: CoordinateArray,
    triangulate_face_: typing.Callable,
) -> tuple[
    tuple[CoordinateArray, TriangleArray],
    tuple[CoordinateArray, CoordinateArray, TriangleArray],
]:
    """Determines the triangulation of the face and edges of a shape.

    Parameters
    ----------
    polygon_vertices : np.ndarray
        Nx2 array of vertices of shape to be triangulated

    Returns
    -------
    face_tri : tuple[np.ndarray, np.ndarray]
        Tuple of vertices and triangles of the face.
    edge_tri : tuple[np.ndarray, np.ndarray, np.ndarray]
        Tuple of vertices, offsets, and triangles of the edges.
    """
    data2d, axis, value = find_planar_axis(polygon_vertices)

    if not len(data2d) or is_collinear(data2d):
        face_tri = (
            np.empty((0, polygon_vertices.shape[1]), dtype=np.float32),
            np.empty((0, 3), dtype=np.int32),
        )
        edge_tri = triangulate_edge(polygon_vertices, closed=True)
        return face_tri, edge_tri  # type: ignore[return-value]

    if _triangulate_dispatch.is_convex(data2d):
        vertices, triangles = _fan_triangulation(data2d)
        face_tri = (
            _fix_vertices_if_needed(vertices, axis=axis, value=value),
            triangles,
        )
        edge_tri = triangulate_edge(polygon_vertices, closed=True)
        return face_tri, edge_tri

    raw_vertices, edges = _triangulate_dispatch.normalize_vertices_and_edges(
        data2d, close=True
    )

    vertices, triangles = triangulate_face_(
        raw_vertices.copy(), edges.copy(), polygon_vertices
    )

    face_tri_ = (
        _fix_vertices_if_needed(vertices, axis=axis, value=value),
        triangles,
    )

    if len(edges) == len(polygon_vertices):
        # There is no removed edge
        edge_tri = triangulate_edge(polygon_vertices, closed=True)
    else:
        # There is at least one removed edge
        edge_tri = reconstruct_and_triangulate_edge(raw_vertices, edges)

    return face_tri_, edge_tri


@overload
def reconstruct_and_triangulate_edge(
    vertices: CoordinateArray2D, edges: EdgeArray
) -> tuple[CoordinateArray2D, CoordinateArray2D, TriangleArray]: ...


@overload
def reconstruct_and_triangulate_edge(
    vertices: CoordinateArray3D, edges: EdgeArray
) -> tuple[CoordinateArray3D, CoordinateArray3D, TriangleArray]: ...


def reconstruct_and_triangulate_edge(
    vertices: CoordinateArray, edges: EdgeArray
) -> tuple[CoordinateArray, CoordinateArray, TriangleArray]:
    """Triangulate edges when vertices form multiple disjoint polygons.

    Parameters
    ----------
    vertices: np.ndarray
        Nx2 or Nx3 array of vertices of shape to be triangulated.
        Cannot contain duplicated vertices.
    edges: np.ndarray
        list of edges encoded as vertices indices.
        Cannot contain duplicated vertices.

    Returns
    -------
    edges_tri: tuple[ndarray, ndarray, ndarray]
        vertices, offsets, and triangles of the edge triangulation(s).
    """
    polygon_list = _triangulate_dispatch.reconstruct_polygons_from_edges(
        vertices, edges
    )
    centers_list = []
    offset_list = []
    triangles_list = []
    offset_idx = 0
    for polygon in polygon_list:
        centers, offset, triangles = triangulate_edge(polygon, closed=True)
        centers_list.append(centers)
        offset_list.append(offset)
        triangles_list.append(triangles + offset_idx)
        offset_idx += len(centers)
    return (
        np.concatenate(centers_list),
        np.concatenate(offset_list),
        np.concatenate(triangles_list),
    )


def triangulate_face(
    polygon_vertices: CoordinateArray2D,
    triangulate_face_: typing.Callable,
) -> tuple[CoordinateArray2D, TriangleArray]:
    """Determines the triangulation of the face of a shape.

    Parameters
    ----------
    polygon_vertices : np.ndarray
        Nx2 array of vertices of shape to be triangulated

    Returns
    -------
    vertices : np.ndarray
        Mx2 array vertices of the triangles.
    triangles : np.ndarray
        Px3 array of the indices of the vertices that will form the
        triangles of the triangulation
    """
    if _triangulate_dispatch.is_convex(polygon_vertices):
        return _fan_triangulation(polygon_vertices)

    raw_vertices, edges = _triangulate_dispatch.normalize_vertices_and_edges(
        polygon_vertices, close=True
    )
    return triangulate_face_(raw_vertices, edges, polygon_vertices)


@overload
def triangulate_face_vispy(
    raw_vertices: CoordinateArray2D,
    edges: EdgeArray,
    polygon_vertices: CoordinateArray,
) -> tuple[CoordinateArray2D, TriangleArray]: ...


@overload
def triangulate_face_vispy(
    raw_vertices: CoordinateArray3D,
    edges: EdgeArray,
    polygon_vertices: CoordinateArray,
) -> tuple[CoordinateArray3D, TriangleArray]: ...


def triangulate_face_vispy(
    raw_vertices: CoordinateArray,
    edges: EdgeArray,
    polygon_vertices: CoordinateArray,
) -> tuple[CoordinateArray, TriangleArray]:
    try:
        tri = Triangulation(raw_vertices, edges)
        tri.triangulate()
        vertices, triangles = tri.pts, tri.tris
    except Exception as e:  # pragma: no cover
        path, text_path = _save_failed_triangulation(
            raw_vertices, backend='vispy'
        )
        raise RuntimeError(
            f'Triangulation failed. Data saved to {path} and {text_path}'
        ) from e

    triangles = triangles.astype(np.uint32)

    return vertices, triangles


@overload
def triangulate_face_triangle(
    raw_vertices: CoordinateArray2D,
    edges: EdgeArray,
    polygon_vertices: CoordinateArray,
) -> tuple[CoordinateArray2D, TriangleArray]: ...


@overload
def triangulate_face_triangle(
    raw_vertices: CoordinateArray3D,
    edges: EdgeArray,
    polygon_vertices: CoordinateArray,
) -> tuple[CoordinateArray3D, TriangleArray]: ...


def triangulate_face_triangle(
    raw_vertices: CoordinateArray,
    edges: EdgeArray,
    polygon_vertices: CoordinateArray,
) -> tuple[CoordinateArray, TriangleArray]:
    res = triangulate({'vertices': raw_vertices, 'segments': edges}, opts='p')
    vertices = res['vertices']
    raw_triangles = res['triangles']
    # triangle's constrained Delaunay triangulation
    # returns triangles inside the hole. (perhaps in case you want
    # to render holes but in a different color, for example.) In our case,
    # we want to get rid of them, so we cull them with some NumPy
    # calculations
    triangles = _cull_triangles_not_in_poly(
        vertices, raw_triangles, polygon_vertices
    )
    triangles = triangles.astype(np.uint32)

    return vertices, triangles


@typing.overload
def triangulate_edge(
    path: CoordinateArray2D, closed: bool = False
) -> tuple[CoordinateArray2D, CoordinateArray2D, TriangleArray]: ...


@typing.overload
def triangulate_edge(
    path: CoordinateArray3D, closed: bool = False
) -> tuple[CoordinateArray3D, CoordinateArray3D, TriangleArray]: ...


def triangulate_edge(
    path: CoordinateArray, closed: bool = False
) -> tuple[CoordinateArray, CoordinateArray, TriangleArray]:
    """Determines the triangulation of a path.

    The resulting `offsets` can multiplied by a `width` scalar and be added
    to the resulting `centers` to generate the vertices of the triangles for
    the triangulation, i.e. `vertices = centers + width*offsets`. Using the
    `centers` and `offsets` representation thus allows for the computed
    triangulation to be independent of the line width.

    Parameters
    ----------
    path : np.ndarray
        Nx2 or Nx3 array of central coordinates of path to be triangulated
    closed : bool
        Bool which determines if the path is closed or not.

    Returns
    -------
    centers : np.ndarray
        Mx2 or Mx3 array central coordinates of path triangles.
    offsets : np.ndarray
        Mx2 or Mx3 array of the offsets to the central coordinates that need to
        be scaled by the line width and then added to the centers to
        generate the actual vertices of the triangulation
    triangles : np.ndarray
        Px3 array of the indices of the vertices that will form the
        triangles of the triangulation
    """

    path = np.asanyarray(path)

    # Remove any equal adjacent points
    if len(path) > 2:
        idx = np.concatenate([[True], ~np.all(path[1:] == path[:-1], axis=-1)])
        clean_path = path[idx].copy()

        if clean_path.shape[0] == 1:
            clean_path = np.concatenate((clean_path, clean_path), axis=0)
    else:
        clean_path = path

    if clean_path.shape[-1] == 2:
        centers, offsets, triangles = (
            _triangulate_dispatch.generate_2D_edge_meshes(
                np.asarray(clean_path, dtype=np.float32), closed=closed
            )
        )
    else:
        centers, offsets, triangles = generate_tube_meshes(
            clean_path, closed=closed
        )

    # offsets[2,1] = -0.5
    return centers, offsets, triangles


def generate_tube_meshes(path, closed=False, tube_points=10):
    """Generates list of mesh vertices and triangles from a path

    Adapted from vispy.visuals.TubeVisual
    https://github.com/vispy/vispy/blob/main/vispy/visuals/tube.py

    Parameters
    ----------
    path : (N, 3) array
        Vertices specifying the path.
    closed : bool
        Bool which determines if the path is closed or not.
    tube_points : int
        The number of points in the circle-approximating polygon of the
        tube's cross section.

    Returns
    -------
    centers : (M, 3) array
        Vertices of all triangles for the lines
    offsets : (M, D) array
        offsets of all triangles for the lines
    triangles : (P, 3) array
        Vertex indices that form the mesh triangles
    """
    points = np.array(path).astype(float)

    if closed and not np.array_equal(points[0], points[-1]):
        points = np.concatenate([points, [points[0]]], axis=0)

    _tangents, normals, binormals = _frenet_frames(points, closed)

    segments = len(points) - 1

    # get the positions of each vertex
    grid = np.zeros((len(points), tube_points, 3))
    grid_off = np.zeros((len(points), tube_points, 3))
    for i in range(len(points)):
        pos = points[i]
        normal = normals[i]
        binormal = binormals[i]

        # Add a vertex for each point on the circle
        v = np.arange(tube_points, dtype=float) / tube_points * 2 * np.pi
        cx = -1.0 * np.cos(v)
        cy = np.sin(v)
        grid[i] = pos
        grid_off[i] = cx[:, np.newaxis] * normal + cy[:, np.newaxis] * binormal

    # construct the mesh
    indices: list[tuple[int, int, int]] = []
    for i, j in itertools.product(range(segments), range(tube_points)):
        ip = (i + 1) % segments if closed else i + 1
        jp = (j + 1) % tube_points

        index_a = i * tube_points + j
        index_b = ip * tube_points + j
        index_c = ip * tube_points + jp
        index_d = i * tube_points + jp

        indices.extend(
            ([index_a, index_b, index_d], [index_b, index_c, index_d])  # type: ignore[arg-type]
        )
    triangles = np.array(indices, dtype=np.uint32)

    centers = grid.reshape(grid.shape[0] * grid.shape[1], 3)
    offsets = grid_off.reshape(grid_off.shape[0] * grid_off.shape[1], 3)

    return centers, offsets, triangles


def path_to_mask(
    mask_shape: npt.NDArray, vertices: npt.NDArray
) -> npt.NDArray[np.bool_]:
    """Converts a path to a boolean mask with `True` for points lying along
    each edge.

    Parameters
    ----------
    mask_shape : array (2,)
        Shape of mask to be generated.
    vertices : array (N, 2)
        Vertices of the path.

    Returns
    -------
    mask : np.ndarray
        Boolean array with `True` for points along the path

    """
    mask_shape = np.asarray(mask_shape, dtype=int)
    mask = np.zeros(mask_shape, dtype=bool)

    vertices = np.round(np.clip(vertices, 0, mask_shape - 1)).astype(int)

    # remove identical, consecutive vertices
    duplicates = np.all(np.diff(vertices, axis=0) == 0, axis=-1)
    duplicates = np.concatenate(([False], duplicates))
    vertices = vertices[~duplicates]

    iis, jjs = [], []
    for v1, v2 in itertools.pairwise(vertices):
        ii, jj = line(*v1, *v2)
        iis.extend(ii.tolist())
        jjs.extend(jj.tolist())

    mask[iis, jjs] = 1

    return mask


def poly_to_mask(
    mask_shape: npt.ArrayLike, vertices: npt.ArrayLike
) -> npt.NDArray[np.bool_]:
    """Converts a polygon to a boolean mask with `True` for points
    lying inside the shape. Uses the bounding box of the vertices to reduce
    computation time.

    Parameters
    ----------
    mask_shape : np.ndarray | tuple
        1x2 array of shape of mask to be generated.
    vertices : np.ndarray
        Nx2 array of the vertices of the polygon.

    Returns
    -------
    mask : np.ndarray
        Boolean array with `True` for points inside the polygon
    """
    return polygon2mask(mask_shape, vertices)


def grid_points_in_poly(shape, vertices):
    """Converts a polygon to a boolean mask with `True` for points
    lying inside the shape. Loops through all indices in the grid

    Parameters
    ----------
    shape : np.ndarray | tuple
        1x2 array of shape of mask to be generated.
    vertices : np.ndarray
        Nx2 array of the vertices of the polygon.

    Returns
    -------
    mask : np.ndarray
        Boolean array with `True` for points inside the polygon
    """
    points = np.array(
        [(x, y) for x in range(shape[0]) for y in range(shape[1])], dtype=int
    )
    inside = points_in_poly(points, vertices)
    mask = inside.reshape(shape)
    return mask


def points_in_poly(points, vertices):
    """Tests points for being inside a polygon using the ray casting algorithm

    Parameters
    ----------
    points : np.ndarray
        Mx2 array of points to be tested
    vertices : np.ndarray
        Nx2 array of the vertices of the polygon.

    Returns
    -------
    inside : np.ndarray
        Length M boolean array with `True` for points inside the polygon
    """
    n_verts = len(vertices)
    inside = np.zeros(len(points), dtype=bool)
    j = n_verts - 1
    for i in range(n_verts):
        # Determine if a horizontal ray emanating from the point crosses the
        # line defined by vertices i-1 and vertices i.
        cond_1 = np.logical_and(
            vertices[i, 1] <= points[:, 1], points[:, 1] < vertices[j, 1]
        )
        cond_2 = np.logical_and(
            vertices[j, 1] <= points[:, 1], points[:, 1] < vertices[i, 1]
        )
        cond_3 = np.logical_or(cond_1, cond_2)
        d = vertices[j] - vertices[i]
        # Prevents floating point imprecision from generating false positives
        tolerance = 1e-12
        d = np.where(abs(d) < tolerance, 0, d)
        if d[1] == 0:
            # If y vertices are aligned avoid division by zero
            cond_4 = d[0] * (points[:, 1] - vertices[i, 1]) > 0
        else:
            cond_4 = points[:, 0] < (
                d[0] * (points[:, 1] - vertices[i, 1]) / d[1] + vertices[i, 0]
            )
        cond_5 = np.logical_and(cond_3, cond_4)
        inside[cond_5] = 1 - inside[cond_5]
        j = i

    # If the number of crossings is even then the point is outside the polygon,
    # if the number of crossings is odd then the point is inside the polygon

    return inside


def extract_shape_type(data, shape_type=None):
    """Separates shape_type from data if present, and returns both.

    Parameters
    ----------
    data : Array | Tuple(Array,str) | List[Array | Tuple(Array, str)] | Tuple(List[Array], str)
        list or array of vertices belonging to each shape, optionally containing shape type strings
    shape_type : str | None
        metadata shape type string, or None if none was passed

    Returns
    -------
    data : Array | List[Array]
        list or array of vertices belonging to each shape
    shape_type : List[str] | None
        type of each shape in data, or None if none was passed
    """
    # Tuple for one shape or list of shapes with shape_type
    if isinstance(data, tuple):
        shape_type = data[1]
        data = data[0]
    # List of (vertices, shape_type) tuples
    elif len(data) != 0 and all(isinstance(datum, tuple) for datum in data):
        shape_type = [datum[1] for datum in data]
        data = [datum[0] for datum in data]
    return data, shape_type


def get_default_shape_type(current_type):
    """If all shapes in current_type are of identical shape type,
       return this shape type, else "polygon" as lowest common
       denominator type.

    Parameters
    ----------
    current_type : list of str
        list of current shape types

    Returns
    -------
    default_type : str
        default shape type
    """
    default = 'polygon'
    if not current_type:
        return default
    first_type = current_type[0]
    if all(shape_type == first_type for shape_type in current_type):
        return first_type
    return default


def get_shape_ndim(data):
    """Checks whether data is a list of the same type of shape, one shape, or
    a list of different shapes and returns the dimensionality of the shape/s.

    Parameters
    ----------
    data : (N, ) list of array
        List of shape data, where each element is an (N, D) array of the
        N vertices of a shape in D dimensions.

    Returns
    -------
    ndim : int
        Dimensionality of the shape/s in data
    """
    # list of all the same shapes
    if np.array(data, dtype=object).ndim == 3:
        ndim = np.array(data).shape[2]
    # just one shape
    elif np.array(data[0]).ndim == 1:
        ndim = np.array(data).shape[1]
    # list of different shapes
    else:
        ndim = np.array(data[0]).shape[1]
    return ndim


def number_of_shapes(data):
    """Determine number of shapes in the data.

    Parameters
    ----------
    data : list or np.ndarray
        Can either be no shapes, if empty, a
        single shape or a list of shapes.

    Returns
    -------
    n_shapes : int
        Number of new shapes
    """
    if len(data) == 0:
        # If no new shapes
        n_shapes = 0
    elif np.array(data[0]).ndim == 1:
        # If a single array for a shape
        n_shapes = 1
    else:
        n_shapes = len(data)

    return n_shapes


def validate_num_vertices(
    data, shape_type, min_vertices=None, valid_vertices=None
):
    """Raises error if a shape in data has invalid number of vertices.

    Checks whether all shapes in data have a valid number of vertices
    for the given shape type and vertex information. Rectangles and
    ellipses can have either 2 or 4 vertices per shape,
    lines can have only 2, while paths and polygons have a minimum
    number of vertices, but no maximum.

    One of valid_vertices or min_vertices must be passed to the
    function.

    Parameters
    ----------
    data : Array | Tuple(Array,str) | List[Array | Tuple(Array, str)] | Tuple(List[Array], str)
        List of shape data, where each element is either an (N, D) array of the
        N vertices of a shape in D dimensions or a tuple containing an array of
        the N vertices and the shape_type string. Can be an 3-dimensional array
        if each shape has the same number of vertices.
    shape_type : str
        Type of shape being validated (for detailed error message)
    min_vertices : int or None
        Minimum number of vertices for the shape type, by default None
    valid_vertices : Tuple(int) or None
        Valid number of vertices for the shape type in data, by default None

    Raises
    ------
    ValueError
        Raised if a shape is found with invalid number of vertices
    """
    n_shapes = number_of_shapes(data)
    # single array of vertices
    if n_shapes == 1 and len(np.array(data).shape) == 2:
        # wrap in extra dimension so we can iterate through shape not vertices
        data = [data]
    for shape in data:
        if (valid_vertices and len(shape) not in valid_vertices) or (
            min_vertices and len(shape) < min_vertices
        ):
            raise ValueError(
                trans._(
                    '{shape_type} {shape} has invalid number of vertices: {shape_length}.',
                    deferred=True,
                    shape_type=shape_type,
                    shape=shape,
                    shape_length=len(shape),
                )
            )


def perpendicular_distance(
    point: npt.NDArray, line_start: npt.NDArray, line_end: npt.NDArray
) -> float:
    """Calculate the perpendicular distance of a point to a given euclidean line.

    Calculates the shortest distance of a point to a euclidean line defined by a line_start point and a line_end point.
    Works up to any dimension.

    Parameters
    ---------
    point : np.ndarray
        A point defined by a numpy array of shape (viewer.ndims,)  which is part of a polygon shape.
    line_start : np.ndarray
        A point defined by a numpy array of shape (viewer.ndims,)  used to define the starting point of a line.
    line_end : np.ndarray
        A point defined by a numpy array of shape (viewer.ndims,)  used to define the end point of a line.

    Returns
    -------
    float
        A float number representing the distance of point to a euclidean line defined by line_start and line_end.
    """

    if np.array_equal(line_start, line_end):
        return float(np.linalg.norm(point - line_start))

    t = np.dot(point - line_end, line_start - line_end) / np.dot(
        line_start - line_end, line_start - line_end
    )
    return float(
        np.linalg.norm(t * (line_start - line_end) + line_end - point)
    )


def rdp(vertices: npt.NDArray, epsilon: float) -> npt.NDArray:
    """Reduce the number of vertices that make up a polygon.

    Implementation of the Ramer-Douglas-Peucker algorithm based on:
    https://github.com/fhirschmann/rdp/blob/master/rdp. This algorithm reduces the amounts of points in a polyline or
    in this case reduces the number of vertices in a polygon shape.

    Parameters
    ----------
    vertices : np.ndarray
        A numpy array of shape (n, viewer.ndims) containing the vertices of a polygon shape.
    epsilon : float
        A float representing the maximum distance threshold. When the perpendicular distance of a point to a given line
        is higher, subsequent refinement occurs.

    Returns
    -------
    np.ndarray
        A numpy array of shape (n, viewer.ndims) containing the vertices of a polygon shape.
    """
    max_distance_index = -1
    max_distance = 0.0

    for i in range(1, vertices.shape[0]):
        d = perpendicular_distance(vertices[i], vertices[0], vertices[-1])
        if d > max_distance:
            max_distance_index = i
            max_distance = d

    if epsilon != 0:
        if max_distance > epsilon and epsilon:
            l1 = rdp(vertices[: max_distance_index + 1], epsilon)
            l2 = rdp(vertices[max_distance_index:], epsilon)
            return np.vstack((l1[:-1], l2))

        # This part of the algorithm is actually responsible for removing the datapoints.
        return np.vstack((vertices[0], vertices[-1]))

    # When epsilon is 0, avoid removing datapoints
    return vertices


def _save_failed_triangulation(
    data: np.ndarray, target_dir: str | None = None, backend: str = ''
) -> tuple[str, str]:
    """Save data to temporary files for debugging.

    This function saves input data when triangulation fails.
    It saves the same data to both a .npz file and a .txt file within the
    temporary directory, and returns the paths to the saved files.

    Parameters
    ----------
    data : np.ndarray
        The data to save.
    target_dir: str or None
        Path to the directory where the files will be saved.
        If None, the default
    backend: str, options
        The backend used for triangulation. This is used to generate the
        filename prefix.

    Returns
    -------
    tuple[str, str]
        The paths to the saved files.

    Notes
    -----
    Use TMPDIR environment variable to set the temporary directory.
    """
    with tempfile.NamedTemporaryFile(
        delete=False,
        suffix='.npz',
        prefix=f'napari_{backend}_triang_',
        dir=target_dir,
    ) as binary_file:
        np.savez(binary_file, data=data)
    with tempfile.NamedTemporaryFile(
        delete=False,
        suffix='.txt',
        prefix=f'napari_{backend}_triang_',
        mode='w',
        dir=target_dir,
    ) as text_file:
        np.savetxt(text_file, data)

    return binary_file.name, text_file.name
