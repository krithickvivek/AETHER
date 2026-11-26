"""One error shape for every failure: {"error": {"code": ..., "message": ..., "details": ...}}.

Rules: messages never contain tracebacks, secrets or absolute paths (text from saved data is passed through
scrub()); domain results such as a failed or cancelled run are normal run state, not API errors. An unexpected
exception is a generic 500 and is logged by the server, not sent to the client.
"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from aether_api import repository as repo
from aether_api.presenters import InvalidOverride, scrub

# domain exception -> (HTTP status, error code, default message)
DOMAIN_ERRORS = {
    repo.RunNotFound: (404, "run_not_found", "There is no saved run with this id."),
    repo.RunUnreadable: (422, "run_unreadable", "The saved run could not be read."),
    repo.RunsFolderInvalid: (503, "runs_folder_invalid", "The runs folder is not usable."),
    repo.StorageUnavailable: (503, "storage_unavailable", "The data could not be read from disk."),
    repo.HistoryEntryNotFound: (404, "history_entry_not_found", "There is no saved history entry with this id."),
    repo.FileNotRecorded: (404, "file_not_found", "This run has no generated file with that path."),
    repo.InvalidFilePath: (422, "invalid_path", "That is not a valid project file path."),
    repo.FileNotText: (422, "file_not_text", "The file is not valid UTF-8 text."),
    InvalidOverride: (422, "invalid_override", "That template cannot be chosen for this requirement."),
}
HTTP_CODES = {404: ("not_found", "No such API resource."), 405: ("method_not_allowed", "This API is read-only: only GET is supported.")}


def error_response(status: int, code: str, message: str, details=None, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, "details": details}}, status_code=status,
                        headers=headers)


def _domain_handler(status: int, code: str, default: str):
    async def handler(request: Request, error: Exception) -> JSONResponse:
        message = scrub(getattr(error, "message", "") or str(error)) or default
        return error_response(status, code, message)
    return handler


async def _history_unavailable(request: Request, error: repo.HistoryUnavailable) -> JSONResponse:
    if error.state == "damaged":
        return error_response(422, "history_damaged", "The history file is damaged, so it cannot be shown.")
    return error_response(503, "history_unavailable", "The history file could not be read.")


async def _validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
    details = [{"location": ".".join(str(part) for part in item["loc"]), "message": item["msg"]}
               for item in error.errors()]  # the offending input is deliberately not echoed back
    return error_response(422, "invalid_request", "The request is not valid.", details)


async def _http_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
    code, message = HTTP_CODES.get(error.status_code, ("http_error", "The request could not be completed."))
    return error_response(error.status_code, code, message, headers=getattr(error, "headers", None))


async def _unexpected(request: Request, error: Exception) -> JSONResponse:
    return error_response(500, "internal_error", "Something went wrong. Nothing was changed.")


def install_error_handlers(app: FastAPI) -> None:
    for exception, (status, code, default) in DOMAIN_ERRORS.items():
        app.add_exception_handler(exception, _domain_handler(status, code, default))
    app.add_exception_handler(repo.HistoryUnavailable, _history_unavailable)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _unexpected)
