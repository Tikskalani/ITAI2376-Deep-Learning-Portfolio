"""Print a fresh VAPID key pair for .env.

    python -m scripts.vapid_keys

Paste the two lines into .env. Never commit them. Generating a new pair
invalidates every existing push subscription (the PWA re-subscribes on
its next open).
"""

from __future__ import annotations

from core.surfaces.webpush import generate_vapid_keys


def main() -> None:
    keys = generate_vapid_keys()
    print(f"ATLAS_VAPID_PUBLIC_KEY={keys.public_key}")
    print(f"ATLAS_VAPID_PRIVATE_KEY={keys.private_key}")


if __name__ == "__main__":
    main()
