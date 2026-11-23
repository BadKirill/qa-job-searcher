import http.client
import json


def check(host, path, expected):
    connection = http.client.HTTPSConnection(host, timeout=20)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        if response.status != expected:
            raise RuntimeError(f"{host}{path}: expected {expected}, got {response.status}")
        return {"host": host, "path": path, "status": response.status}
    finally:
        connection.close()


def main():
    checks = [("/", 200), ("/api/session", 200)]
    checks += [
        (path, 401)
        for path in (
            "/api/jobs",
            "/api/profile",
            "/api/admin/users",
            "/api/admin/diagnostics",
            "/api/tasks/unknown",
            "/api/artifacts/unknown/download",
            "/api/events",
        )
    ]
    checks += [
        (path, 404)
        for path in (
            "/health/ready",
            "/health/live",
            "/docs",
            "/openapi.json",
            "/redoc",
            "/metrics",
            "/.env",
            "/.git/config",
            "/api/setup",
        )
    ]
    results = [check("getcareer.app", path, status) for path, status in checks]
    results += [
        check("api.getcareer.app", path, 404)
        for path in ("/", "/api/session", "/api/jobs", "/api/login", "/health/ready", "/docs")
    ]
    print(json.dumps({"public_security_checks": results}))


if __name__ == "__main__":
    main()
