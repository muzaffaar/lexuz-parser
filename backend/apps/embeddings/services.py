"""pgvector helpers (TZ 66-71).

`embedding` is an untyped `vector` column because dimensions differ per EmbeddingProfile. That means an
index must be built over a *cast* (`embedding::vector(N)`) and restricted to one profile with a partial
predicate, and queries must repeat the same cast or the planner will not use the index.
"""
import uuid

from django.db import connection, transaction

from .models import DistanceMetric, EmbeddingProfile

_OPCLASS = {DistanceMetric.COSINE: "vector_cosine_ops", DistanceMetric.L2: "vector_l2_ops", DistanceMetric.INNER_PRODUCT: "vector_ip_ops"}
_OPERATOR = {DistanceMetric.COSINE: "<=>", DistanceMetric.L2: "<->", DistanceMetric.INNER_PRODUCT: "<#>"}


def index_name(profile: EmbeddingProfile) -> str:
    return f"chunkemb_hnsw_{profile.id.hex[:16]}"


def ensure_profile_index(profile: EmbeddingProfile, *, concurrently: bool = False) -> str:
    """Create the partial HNSW index for a profile. Use concurrently=True on a live system (it must then
    run outside a transaction). Final m/ef_construction come from the benchmark (TZ 70-71)."""
    name = index_name(profile)
    uuid.UUID(str(profile.id))  # the id is interpolated into DDL below (parameters are not allowed there)
    sql = (
        f"CREATE INDEX {'CONCURRENTLY ' if concurrently else ''}IF NOT EXISTS {name} ON embeddings_chunkembedding "
        f"USING hnsw ((embedding::vector({int(profile.dimension)})) {_OPCLASS[profile.distance_metric]}) "
        f"WITH (m = {int(profile.hnsw_m)}, ef_construction = {int(profile.hnsw_ef_construction)}) "
        f"WHERE embedding_profile_id = '{profile.id}'"
    )
    with connection.cursor() as cur:
        cur.execute(sql)
    return name


def nearest(profile: EmbeddingProfile, query_vector, *, limit: int = 20):
    """Nearest chunks for one profile -> [(chunk_id, distance)]. Row Level Security applies automatically
    for the yurist_api role, so a tenant only ever ranks official + own chunks."""
    dim = int(profile.dimension)
    op = _OPERATOR[profile.distance_metric]
    vec = "[" + ",".join(repr(float(x)) for x in query_vector) + "]"
    sql = (
        f"SELECT chunk_id, embedding::vector({dim}) {op} %s::vector({dim}) AS distance "
        f"FROM embeddings_chunkembedding WHERE embedding_profile_id = %s "
        f"ORDER BY embedding::vector({dim}) {op} %s::vector({dim}) LIMIT %s"
    )
    with transaction.atomic(), connection.cursor() as cur:
        # keep scanning when metadata filters / RLS discard neighbours (pgvector >= 0.8)
        cur.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        cur.execute(sql, [vec, profile.id, vec, limit])
        return cur.fetchall()
