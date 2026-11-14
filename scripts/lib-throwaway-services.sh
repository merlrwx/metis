# shellcheck shell=bash
# Start disposable PostgreSQL (pgvector) and optionally Redis containers on random
# loopback ports. Tests DELETE rows, so they must never use the Compose project's
# database or volumes, which may hold your real local data.
throwaway_id="metis-test-$$-$RANDOM"
throwaway_storage="$(mktemp -d)"

throwaway_cleanup() {
  docker rm -f "pg-$throwaway_id" "redis-$throwaway_id" >/dev/null 2>&1 || true
  rm -rf "$throwaway_storage"
}
trap throwaway_cleanup EXIT

start_throwaway_postgres() {
  docker run -d --rm --name "pg-$throwaway_id" -e POSTGRES_DB=metis_test \
    -e POSTGRES_USER=metis -e POSTGRES_PASSWORD=metis-throwaway \
    -p 127.0.0.1::5432 pgvector/pgvector:pg17 >/dev/null
  local port
  port="$(docker port "pg-$throwaway_id" 5432/tcp | head -1 | sed 's/.*://')"
  for _ in $(seq 60); do
    if docker exec "pg-$throwaway_id" pg_isready -U metis -d metis_test >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  # The entrypoint restarts PostgreSQL once after initialisation.
  sleep 2
  docker exec "pg-$throwaway_id" pg_isready -U metis -d metis_test >/dev/null
  export DATABASE_URL="postgresql+psycopg://metis:metis-throwaway@127.0.0.1:${port}/metis_test"
  export TEST_DATABASE_URL="$DATABASE_URL"
}

start_throwaway_redis() {
  docker run -d --rm --name "redis-$throwaway_id" -p 127.0.0.1::6379 redis:7-alpine >/dev/null
  local port
  port="$(docker port "redis-$throwaway_id" 6379/tcp | head -1 | sed 's/.*://')"
  export REDIS_URL="redis://127.0.0.1:${port}/15"
}

export OBJECT_STORAGE_BACKEND=local
export OBJECT_STORAGE_LOCAL_DIR="$throwaway_storage"
export EMBEDDING_PROVIDER=hashing
export METIS_AUTH_SECRET_KEY="metis-local-test-secret-key-with-at-least-32-chars"
export METIS_BOOTSTRAP_TOKEN="metis-local-test-bootstrap-token"
