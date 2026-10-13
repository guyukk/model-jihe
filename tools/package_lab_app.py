"""Create the separate desktop LAB calibration app package."""
from pathlib import Path
import zipfile


def main():
    root = Path(__file__).resolve().parent.parent
    src = root / "calibration" / "lab_app"
    dest = root / "dist" / "maix-labcal-v1.0.0.zip"
    files = {"app.yaml": src / "app.yaml", "main.py": src / "main.py",
             "lab_color.py": root / "lab_color.py", "app.png": root / "app.png"}
    missing = [str(path) for path in files.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(", ".join(missing))
    dest.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, path in files.items():
            archive.write(path, name)
    print(dest)


if __name__ == "__main__":
    main()
