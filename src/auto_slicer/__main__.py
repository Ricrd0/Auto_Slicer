from __future__ import annotations

import uvicorn


def main() -> None:
    uvicorn.run("auto_slicer.app:app", host="0.0.0.0", port=8080, factory=False)


if __name__ == "__main__":
    main()
