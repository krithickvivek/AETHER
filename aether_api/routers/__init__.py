"""The API's route modules. Each is a thin read: load through the repository, present, return."""

from aether_api.schemas import ErrorResponse

# Documented on every endpoint so the OpenAPI contract (and generated TypeScript types) include the error shape.
ERRORS = {
    403: {"model": ErrorResponse, "description": "Host or Origin not allowed."},
    404: {"model": ErrorResponse, "description": "Not found."},
    422: {"model": ErrorResponse, "description": "Invalid request, or a saved record that cannot be read."},
    503: {"model": ErrorResponse, "description": "The data could not be read from disk."},
}
