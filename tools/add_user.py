#!/usr/bin/env python3
"""Add or update a dashboard operator in users.json.

Usage:
    python3 add_user.py NAME [PIN] [--email ADDR] [--display "Full Name"]
    python3 add_user.py --delete NAME
    python3 add_user.py --list

Run on the tower box (or set USERS_FILE). PINs are salted-SHA256 hashed;
the plaintext PIN is printed once — write it down, it is not stored.
The email/display name are stored in the clear so the dashboard can offer a
"Contact <operator>" link when someone else holds the single-operator lock.
"""
import json, sys, secrets, hashlib, os

USERS_FILE = os.environ.get('USERS_FILE', '/home/tower-two/wgxc-dashboard/users.json')

def load():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE) as f:
            return json.load(f)
    return {}

def save(users):
    with open(USERS_FILE, 'w') as f:
        json.dump(users, f, indent=2)
    os.chmod(USERS_FILE, 0o600)

def set_user(name, pin=None, email=None, display=None):
    name = name.strip().lower()
    users = load()
    rec = dict(users.get(name, {}))
    if pin is not None or 'pin_sha256' not in rec:
        pin = pin if pin is not None else ''.join(secrets.choice('0123456789') for _ in range(6))
        salt = secrets.token_hex(8)
        rec['salt'] = salt
        rec['pin_sha256'] = hashlib.sha256((salt + pin).encode()).hexdigest()
    if email is not None:
        rec['email'] = email
    if display is not None:
        rec['name'] = display
    users[name] = rec
    save(users)
    return name, pin

def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        users = load()
        if users:
            print("Current operators:", ", ".join(sorted(users)))
        sys.exit(1)

    if args[0] == '--list':
        for n, r in sorted(load().items()):
            print(f"{n:<12} {r.get('name',''):<24} {r.get('email','')}")
        return

    if args[0] == '--delete':
        name = args[1].strip().lower()
        users = load()
        if users.pop(name, None) is None:
            sys.exit(f"no such user: {name}")
        save(users)
        print(f"deleted {name}")
        return

    # positional: NAME [PIN]; flags: --email, --display
    name = args[0]
    pin = None
    email = None
    display = None
    rest = args[1:]
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == '--email':
            email = rest[i + 1]; i += 2
        elif a == '--display':
            display = rest[i + 1]; i += 2
        else:
            pin = a; i += 1
    name, pin = set_user(name, pin, email, display)
    print(f"user: {name}   PIN: {pin}   email: {email or '(unchanged)'}")

if __name__ == '__main__':
    main()
