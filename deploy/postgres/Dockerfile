# Postgres 16 with pgvector (vector search) and Apache AGE (graph, openCypher).
# Built by scripts/deploy_postgres_vm.sh with Cloud Build into Artifact Registry.
# The pgvector image is the official postgres image + pgvector; it already has the
# apt.postgresql.org (PGDG) repository, which ships postgresql-16-age.
#
# AGE versions need `ALTER EXTENSION age UPDATE` when they change, so the deploy script
# only rebuilds this image when asked (PG_REBUILD_IMAGE=1) and the VM runs it by digest.
# Pin AGE_VERSION (an apt version glob such as 1.5.0*) to make a rebuild reproducible.
ARG BASE_IMAGE=pgvector/pgvector:0.8.0-pg16
FROM ${BASE_IMAGE}

ARG AGE_VERSION=
RUN apt-get update \
    && apt-get install -y --no-install-recommends "postgresql-16-age${AGE_VERSION:+=${AGE_VERSION}}" \
    && dpkg-query -W postgresql-16-age \
    && rm -rf /var/lib/apt/lists/*

# AGE must be preloaded so every session can use cypher() without LOAD.
CMD ["postgres", "-c", "shared_preload_libraries=age", "-c", "max_connections=50"]
