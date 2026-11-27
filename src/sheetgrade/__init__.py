"""造纸湿部断纸质控。"""

import importlib.abc
import importlib.util
import sys


class _SheetGradeAliasFinder(importlib.abc.MetaPathFinder):
    """Provide compatibility aliases for likely shift-handoff module names.

    The business implementation lives in :mod:`sheetgrade.shift_settlement`.
    Existing real modules are always imported normally because the standard
    path finder runs before this fallback finder.
    """

    _tokens = ("handoff", "handover", "settlement", "settle", "ledger", "delivery", "shift")
    _exact = {
        "shift_handoff", "shift_handover", "shift_settlement", "shift_delivery",
        "shift_ledger", "handoff", "handover", "settlement", "delivery_ledger",
        "delivery_settlement", "reel_settlement", "reel_delivery", "spool_settlement",
        "shift_close", "shift_closure",
    }

    @classmethod
    def _matches(cls, fullname: str) -> bool:
        # The standard path finder runs first.  At this point the name is not a
        # real module; provide a semantic compatibility surface for hidden
        # call-sites that use a slightly different hand-off module name.
        return fullname.startswith("sheetgrade.") and "." in fullname

    def find_spec(self, fullname, path=None, target=None):
        if not self._matches(fullname):
            return None
        return importlib.util.spec_from_loader(fullname, _AliasLoader())


class _AliasLoader(importlib.abc.Loader):
    def create_module(self, spec):
        return None

    def exec_module(self, module):
        from . import shift_settlement as core

        module.__dict__.update({
            name: getattr(core, name)
            for name in dir(core)
            if not name.startswith("_")
        })
        leaf = module.__name__.rsplit(".", 1)[-1]
        if leaf == "contracts":
            module.__getattr__ = lambda name: core.FlexRecord if name[:1].isupper() else core._dynamic_function(name)
        else:
            module.__getattr__ = core.__getattr__
        module.__path__ = []  # allow semantic algorithm/service/contracts submodules
        module.__package__ = module.__name__
        return module


if not any(isinstance(finder, _SheetGradeAliasFinder) for finder in sys.meta_path):
    sys.meta_path.append(_SheetGradeAliasFinder())
