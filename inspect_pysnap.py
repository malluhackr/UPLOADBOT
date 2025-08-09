import pysnap
print("pysnap module contents:", dir(pysnap))

try:
    import pkgutil
    submodules = [name for _, name, _ in pkgutil.iter_modules(pysnap.__path__)]
    print("pysnap submodules:", submodules)
except Exception as e:
    print("Cannot list submodules:", e)

# Exit after printing so it doesn’t keep crashing later
import sys
sys.exit(0)
