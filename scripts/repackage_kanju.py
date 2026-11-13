import os, sys, zipfile, struct, zlib, hashlib, subprocess, tempfile

def build():
    HERE = os.path.dirname(os.path.abspath(__file__))
    ROOT = os.path.dirname(HERE)
    out_dir = os.path.join(ROOT, 'static', 'downloads')
    os.makedirs(out_dir, exist_ok=True)
    out_apk = os.path.join(out_dir, 'KanjuAI-0.1.28-CustomServer.apk')
    out_ipa = os.path.join(out_dir, 'KanjuAI-1.0.26-CustomServer.ipa')
    print("Kanju modified packages are maintained at:", out_dir)

if __name__ == '__main__':
    build()
