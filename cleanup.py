#!/usr/bin/env python3
"""
Windows 11 Docker — Full Aggressive Cleanup (ადგილის გათავისუფლება)
"""

import subprocess
import shutil
from pathlib import Path

DATA_DIR = Path.home() / "win11_sessions"
CONFIG_FILE = Path.home() / ".win11_sessions.json"

def run(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)

def banner(msg):
    print(f"\n{'─'*80}")
    print(f"  {msg}")
    print(f"{'─'*80}")

def check_disk_usage():
    banner("📊 მიმდინარე დისკის მდგომარეობა")
    run("df -h ~")

def remove_all_win11_containers():
    banner("🐳 ყველა win11 კონტეინერის წაშლა")
    result = run("docker ps -a --format '{{.Names}}' | grep '^win11_'")
    containers = [c.strip() for c in result.stdout.splitlines() if c.strip()]
    
    for c in containers:
        print(f"  Removing container: {c}")
        run(f"docker stop {c} 2>/dev/null")
        run(f"docker rm -f {c} 2>/dev/null")
    print(f"  ✓ წაშლილია {len(containers)} კონტეინერი")

def remove_all_win11_volumes():
    banner("💾 ყველა win11 volume-ის წაშლა")
    result = run("docker volume ls -q | grep -E 'win11'")
    volumes = [v.strip() for v in result.stdout.splitlines() if v.strip()]
    
    for v in volumes:
        print(f"  Removing volume: {v}")
        run(f"docker volume rm -f {v} 2>/dev/null")
    print(f"  ✓ წაშლილია {len(volumes)} volume")

def delete_all_vm_disk_files():
    banner("🗑️  win11_sessions საქაღალდის სრული წაშლა (დიდი დისკის ფაილები)")
    if DATA_DIR.exists():
        size_gb = sum(f.stat().st_size for f in DATA_DIR.rglob('*') if f.is_file()) / (1024**3)
        print(f"  საქაღალდე: {DATA_DIR}")
        print(f"  ზომა: {size_gb:.1f} GB")
        
        confirm = input("\n  გსურს სრულად წაშლა ამ საქაღალდის? (ეს გათავისუფლებს ყველაზე მეტ ადგილს) yes/no: ").strip().lower()
        if confirm == "yes":
            shutil.rmtree(DATA_DIR)
            print("  ✓ win11_sessions საქაღალდე სრულად წაშლილია")
        else:
            print("  გამოტოვებულია.")
    else:
        print("  win11_sessions საქაღალდე არ არსებობს.")

def remove_docker_image():
    banner("🖼️  dockurr/windows image-ის წაშლა (~5-8 GB)")
    confirm = input("  გსურს dockurr/windows image-ის წაშლა? (yes/no): ").strip().lower()
    if confirm == "yes":
        run("docker rmi -f dockurr/windows:latest 2>/dev/null")
        run("docker rmi -f dockurr/windows 2>/dev/null")
        print("  ✓ Docker image წაშლილია")

def prune_docker():
    banner("🧹 Docker-ის სრული გაწმენდა (cache, unused objects)")
    print("  ვაკეთებ docker system prune...")
    run("docker system prune -f --volumes")
    run("docker builder prune -f")

def main():
    print("\n╔════════════════════════════════════════════════════════════╗")
    print("║        FULL AGGRESSIVE CLEANUP — ადგილის გათავისუფლება     ║")
    print("╚════════════════════════════════════════════════════════════╝\n")

    check_disk_usage()

    confirm = input("\n⚠️  გავაგრძელო სრული გაწმენდა? (yes/no): ").strip().lower()
    if confirm != "yes":
        print("გაუქმებულია.")
        return

    remove_all_win11_containers()
    remove_all_win11_volumes()
    delete_all_vm_disk_files()
    remove_docker_image()
    prune_docker()

    if CONFIG_FILE.exists():
        CONFIG_FILE.unlink()
        print("  ✓ კონფიგურაციის ფაილი წაშლილია")

    run("fuser -k 5000/tcp 2>/dev/null")
    print("  ✓ პორტი 5000 გათავისუფლებულია")

    banner("✅ სრული გაწმენდა დასრულებულია")
    print("  შეამოწმე დისკის ადგილი ახლა:")
    check_disk_usage()
    print("\n  თუ კიდევ ბევრი ადგილია დაკავებული — მითხარი, დამატებით გავაკეთებ prune.")

if __name__ == "__main__":
    main()
