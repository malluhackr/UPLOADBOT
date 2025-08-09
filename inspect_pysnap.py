import pysnap

print("pysnap module contents:", dir(pysnap))

# Try to list submodules if possible
try:
    import pkgutil
    submodules = [name for _, name, _ in pkgutil.iter_modules(pysnap.__path__)]
    print("pysnap submodules:", submodules)
except Exception as e:
    print("Cannot list submodules:", e)
