"""Canonical directory inclusion rules, independent of the filesystem and filters.

The deepest matching path wins. These rules decide directory selection only;
existing glob exclusions must still be applied separately and always take
precedence. No path is resolved, opened, or traversed by this module.
"""
from collections.abc import Iterable
import os


def _path(value):
    try:
        value = os.fspath(value)
    except TypeError:
        raise ValueError('O caminho deve ser absoluto e normalizado') from None
    if (not isinstance(value, str) or not value.startswith('/') or '\x00' in value
            or (value != '/' and any(part in ('', '.', '..') for part in value[1:].split('/')))):
        raise ValueError('O caminho deve ser absoluto e normalizado')
    return value


def _inside(path, parent):
    return path == parent or (path.startswith('/') if parent == '/' else path.startswith(parent + '/'))


def _order(path):
    return len(path.split('/')), path


class DirectoryRules:
    """A minimal, immutable-by-interface list of absolute inclusion overrides.

    ``home`` restricts editable rule paths strictly below the user's home. Query
    paths may include the home itself, which can consequently be partial.
    Blocked paths are always excluded, even beneath an included ancestor. An
    explicit positive rule inside a blocked tree is rejected rather than hidden.
    """
    def __init__(self, rules: Iterable[dict], home=None, blocked_paths=()):
        self.home = _path(home) if home is not None else None
        blocked = sorted({_path(path) for path in blocked_paths}, key=_order)
        self.blocked_paths = tuple(path for path in blocked if not any(
            path != ancestor and _inside(path, ancestor) for ancestor in blocked))
        if isinstance(rules, (str, bytes, dict)):
            raise ValueError('As regras devem ser uma lista de seleções de diretório')
        try:
            iterator = iter(rules)
        except TypeError:
            raise ValueError('As regras devem ser uma lista de seleções de diretório') from None
        unique = {}
        for rule in iterator:
            if not isinstance(rule, dict) or set(rule) != {'path', 'included'}:
                raise ValueError('Cada regra precisa de caminho e seleção')
            path = _path(rule['path'])
            included = rule['included']
            if type(included) is not bool:
                raise ValueError('A seleção de diretório deve ser verdadeira ou falsa')
            self._validate_editable(path, included)
            if path in unique and unique[path] != included:
                raise ValueError('Há regras conflitantes para o mesmo diretório')
            unique[path] = included
        canonical = []
        for path in sorted(unique, key=_order):
            inherited = self._lookup(path, canonical)
            if unique[path] != inherited:
                canonical.append((path, unique[path]))
        self._rules = tuple(canonical)

    def _validate_editable(self, path, included):
        if self.home is not None and (path == self.home or not _inside(path, self.home)):
            raise ValueError('Selecione um diretório dentro da pasta pessoal')
        if included and any(_inside(path, blocked) for blocked in self.blocked_paths):
            raise ValueError('Os dados internos do aplicativo não podem entrar no backup')

    @staticmethod
    def _lookup(path, rules):
        included = False
        for parent, value in rules:
            if _inside(path, parent):
                included = value
        return included

    @property
    def rules(self):
        """Return a detached canonical list; callers cannot mutate this object."""
        return [dict(path=path, included=included) for path, included in self._rules]

    def includes(self, path):
        path = _path(path)
        if any(_inside(path, blocked) for blocked in self.blocked_paths):
            return False
        return self._lookup(path, self._rules)

    def state(self, path):
        path = _path(path)
        if any(_inside(path, blocked) for blocked in self.blocked_paths):
            return 'excluded'
        included = self.includes(path)
        if any(parent != path and _inside(parent, path) and value != included
               for parent, value in self._rules):
            return 'partial'
        if included and any(blocked != path and _inside(blocked, path) for blocked in self.blocked_paths):
            return 'partial'
        return 'included' if included else 'excluded'

    def with_toggle(self, path, included):
        """Replace a complete subtree selection and compact redundant overrides."""
        path = _path(path)
        if type(included) is not bool:
            raise ValueError('A seleção de diretório deve ser verdadeira ou falsa')
        self._validate_editable(path, included)
        rules = [dict(path=parent, included=value) for parent, value in self._rules
                 if not _inside(parent, path)]
        rules.append(dict(path=path, included=included))
        return DirectoryRules(rules, home=self.home, blocked_paths=self.blocked_paths).rules
