#include <Python.h>
#include <stdio.h>
#include "pycore_import.h"

/* Includes for frozen modules: */
#include "Python/frozen_modules/importlib._bootstrap.h"
#include "Python/frozen_modules/importlib._bootstrap_external.h"
#include "Python/frozen_modules/zipimport.h"
/* End includes */

uint32_t _Py_next_func_version = 1;

int _Py_Deepfreeze_Init(void) { return 0; }
void _Py_Deepfreeze_Fini(void) {}

static const struct _frozen bootstrap_modules[] = {
    {"_frozen_importlib", _Py_M__importlib__bootstrap, (int)sizeof(_Py_M__importlib__bootstrap)},
    {"_frozen_importlib_external", _Py_M__importlib__bootstrap_external, (int)sizeof(_Py_M__importlib__bootstrap_external)},
    {"zipimport", _Py_M__zipimport, (int)sizeof(_Py_M__zipimport)},
    {0, 0, 0}
};
static const struct _frozen stdlib_modules[] = {
    {0, 0, 0}
};
static const struct _frozen test_modules[] = {
    {0, 0, 0}
};
const struct _frozen *_PyImport_FrozenBootstrap = bootstrap_modules;
const struct _frozen *_PyImport_FrozenStdlib = stdlib_modules;
const struct _frozen *_PyImport_FrozenTest = test_modules;

static const struct _module_alias aliases[] = {
    {"_frozen_importlib", "importlib._bootstrap"},
    {"_frozen_importlib_external", "importlib._bootstrap_external"},
    {0, 0}
};
const struct _module_alias *_PyImport_FrozenAliases = aliases;
const struct _frozen *PyImport_FrozenModules = NULL;

int main(int argc, char **argv)
{
    PyConfig config;
    PyConfig_InitIsolatedConfig(&config);
    config.site_import = 0;
    config.use_hash_seed = 1;
    config.hash_seed = 0;
    config._install_importlib = 1;
    config._init_main = 0;
    config.use_environment = 1;
    config.isolated = 0;
    config.safe_path = 0;
    (void)argc; (void)argv;
    PyStatus status = Py_InitializeFromConfig(&config);
    if (PyStatus_Exception(status)) {
        Py_ExitStatusException(status);
    }
    PyConfig_Clear(&config);

    PyObject *sysmod = PyImport_ImportModule("sys");
    if (sysmod) {
        PyObject *path = PyObject_GetAttrString(sysmod, "path");
        printf("sys.path = %S\n", path);
        Py_DECREF(path);
        Py_DECREF(sysmod);
    }

    PyObject *m = PyImport_ImportModule("encodings");
    if (m == NULL) {
        printf("IMPORT FAILED\n");
        PyErr_Print();
        return 1;
    }
    printf("IMPORT OK\n");
    Py_DECREF(m);
    return 0;
}
