"""Evaluate a parsed Pulsar program into a Song."""
from __future__ import annotations

import inspect
import math
from dataclasses import replace as dc_replace
from fractions import Fraction

from lark import Token, Tree

from . import pattern as P
from . import presets, theory
from .dsp import effects as FX
from .dsp import faust as FZ
from .errors import PulsarError, suggest
from .parser import parse
from .song import Automation, Effect, Instrument, Lfo, Play, Ramp, Section, Song, Track


class PStr(str):
    """A string literal that remembers where it came from (for pattern errors)."""
    loc = None


class Return(Exception):
    def __init__(self, value):
        self.value = value


class Function:
    def __init__(self, name, params, body, env, interp, is_expr):
        self.name, self.params, self.body, self.env, self.interp, self.is_expr = name, params, body, env, interp, is_expr

    def __call__(self, *args, **kwargs):
        return self.interp.call_user(self, list(args), kwargs, None)

    def __repr__(self):
        return f"<fn {self.name}>"


class Env:
    def __init__(self, parent=None):
        self.vars = {}
        self.parent = parent

    def get(self, name):
        e = self
        while e is not None:
            if name in e.vars:
                return e.vars[name]
            e = e.parent
        raise KeyError(name)

    def has(self, name):
        try:
            self.get(name)
            return True
        except KeyError:
            return False

    def set(self, name, value):
        e = self
        while e is not None:
            if name in e.vars:
                e.vars[name] = value
                return
            e = e.parent
        self.vars[name] = value

    def define(self, name, value):
        self.vars[name] = value

    def names(self):
        out, e = set(), self
        while e is not None:
            out |= set(e.vars)
            e = e.parent
        return out


def loc_of(node):
    if isinstance(node, Token):
        return (node.line, node.column)
    meta = getattr(node, "meta", None)
    if meta is not None and not meta.empty:
        return (meta.line, meta.column)
    return None


def to_pattern(v, loc=None, what="pattern"):
    if isinstance(v, P.Pattern):
        return v
    if isinstance(v, str):
        return P.mini(str(v), getattr(v, "loc", None) or loc)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return P.pure({"num": float(v), "notes": [int(v)]})
    if isinstance(v, list):
        return P.slowcat([to_pattern(x, loc) for x in v])
    raise PulsarError(f"expected a {what} (a string like \"bd sd\" or a pattern), got {type_name(v)}", loc)


def type_name(v):
    if isinstance(v, bool):
        return "a boolean"
    if isinstance(v, (int, float, Fraction)):
        return "a number"
    if isinstance(v, str):
        return "a string"
    if isinstance(v, list):
        return "a list"
    if v is None:
        return "none"
    return {P.Pattern: "a pattern", Instrument: "an instrument", Effect: "an effect", Section: "a section",
            Track: "a track", Function: "a function", Ramp: "a ramp", Lfo: "an lfo"}.get(type(v), type(v).__name__)


def num(v, loc=None, what="value"):
    if isinstance(v, bool) or not isinstance(v, (int, float, Fraction)):
        raise PulsarError(f"expected a number for {what}, got {type_name(v)}", loc)
    return v


# -- built-in vocabulary ------------------------------------------------------

EFFECT_POSITIONAL = {"tone": "bass", "reverb": "mix", "delay": "time", "chorus": "mix", "drive": "amount", "crush": "bits",
                     "filter": "cutoff", "lp": "freq", "hp": "freq", "eq": "low", "comp": "threshold",
                     "width": "amount", "phaser": "rate", "tremolo": "rate", "gain": "db", "pan": "pos"}

ENGINE_PARAMS = {"analog": FZ.ANALOG_PARAMS, "fm": FZ.FM_PARAMS, "pluck": FZ.PLUCK_PARAMS}
SHAPE_KEYS = {"wave1", "wave2", "unison", "filter", "mono"}
SYNC_KEYS = {"vib_sync": "vib_rate", "flfo_sync": "flfo_rate", "trem_sync": "trem_rate"}


class Interp:
    def __init__(self, source: str, filename: str = "<song>"):
        self.source = source
        self.filename = filename
        from pathlib import Path as _P
        fp = _P(filename)
        self.basedir = fp.resolve().parent if filename and not filename.startswith("<") else _P.cwd()
        self.song = Song()
        self.section: Section | None = None
        self.globals = Env()
        self._install_builtins()
        self.output: list[str] = []
        self.depth = 0

    # -- entry ---------------------------------------------------------------

    def run(self) -> Song:
        tree = parse(self.source)
        self.exec_block(tree.children, self.globals)
        for name, tr in self.song.tracks.items():
            for k in tr.mixer:
                if k in ("reverb", "delay") or k in presets.MIXER_KEYS:
                    continue
                if k not in self.song.buses:
                    raise PulsarError(f"track {name!r} sends to unknown bus {k!r}", tr.loc,
                                      suggest(k, list(self.song.buses) + ["reverb", "delay"]))
        return self.song

    # -- statements ------------------------------------------------------------

    def exec_block(self, stmts, env):
        for st in stmts:
            if st is None or isinstance(st, Token):
                continue
            self.exec(st, env)

    def exec(self, node: Tree, env: Env):
        kind = node.data
        fn = getattr(self, f"s_{kind}", None)
        if fn is None:
            self.eval(node, env)
            return
        fn(node, env)

    def s_expr_stmt(self, node, env):
        self.eval(node.children[0], env)

    def s_let_stmt(self, node, env):
        name, expr = node.children
        env.define(str(name), self.eval(expr, env))

    def s_assign_stmt(self, node, env):
        target, expr = node.children
        value = self.eval(expr, env)
        if isinstance(target, Tree) and target.data == "name":
            env.set(str(target.children[0]), value)
            return
        if isinstance(target, Tree) and target.data == "getattr":
            obj = self.eval(target.children[0], env)
            attr = str(target.children[1])
            if isinstance(obj, Track):
                self.set_track_attr(obj, attr, value, loc_of(target))
                return
            raise PulsarError(f"can't set .{attr} on {type_name(obj)}", loc_of(target))
        raise PulsarError("can only assign to a name or a track setting like bass.gain", loc_of(target))

    def s_fn_def(self, node, env):
        name, params, body = node.children
        env.define(str(name), Function(str(name), self.params(params, env), body, env, self,
                                       not (isinstance(body, Tree) and body.data == "block")))

    def params(self, node, env):
        if node is None:
            return []
        out = []
        for p in node.children:
            pname, default = p.children
            out.append((str(pname), default))
        return out

    def s_if_stmt(self, node, env):
        cond, block, *rest = node.children
        if self.truthy(self.eval(cond, env)):
            return self.exec_block(block.children, Env(env))
        for clause in rest:
            if clause is None:
                continue
            if clause.data == "elif_clause":
                c, b = clause.children
                if self.truthy(self.eval(c, env)):
                    return self.exec_block(b.children, Env(env))
            else:
                return self.exec_block(clause.children[0].children, Env(env))

    def s_for_stmt(self, node, env):
        name, it, block = node.children
        seq = self.eval(it, env)
        if isinstance(seq, (int, float)) and not isinstance(seq, bool):
            seq = list(range(int(seq)))
        if not isinstance(seq, (list, str)):
            raise PulsarError(f"can't loop over {type_name(seq)}; use a range like 0..4 or a list", loc_of(it))
        for v in seq:
            inner = Env(env)
            inner.define(str(name), v)
            self.exec_block(block.children, inner)

    def s_repeat_stmt(self, node, env):
        count, block = node.children
        n = int(num(self.eval(count, env), loc_of(count), "repeat"))
        for i in range(n):
            inner = Env(env)
            inner.define("i", i)
            self.exec_block(block.children, inner)

    def s_return_stmt(self, node, env):
        raise Return(self.eval(node.children[0], env) if node.children[0] is not None else None)

    def s_setting(self, node, env):
        tok, expr = node.children
        what = str(tok)
        v = self.eval(expr, env)
        loc = loc_of(expr)
        song = self.song
        if what == "tempo":
            if self.section is not None:
                raise PulsarError("tempo can only be set at the top level", loc)
            v = num(v, loc, "tempo")
            if not 20 <= v <= 400:
                raise PulsarError(f"tempo {v} is out of range (20-400 BPM)", loc)
            song.tempo = float(v)
        elif what == "key":
            if not isinstance(v, str):
                raise PulsarError('key takes a string like "A minor" or "F# dorian"', loc)
            try:
                pc, scale = theory.parse_key(v)
            except ValueError as e:
                raise PulsarError(str(e), loc) from None
            if self.section is not None:
                self.section.key = (pc, tuple(scale))
            else:
                song.key_pc, song.scale, song.key_name = pc, tuple(scale), str(v)
        elif what == "swing":
            v = float(num(v, loc, "swing"))
            if not 0 <= v <= 0.9:
                raise PulsarError("swing goes from 0 (straight) to about 0.6 (heavy shuffle)", loc)
            if self.section is not None:
                self.section.swing = v
            else:
                song.swing = v
        elif what == "seed":
            song.seed = int(num(v, loc, "seed"))
        elif what == "loudness":
            v = float(num(v, loc, "loudness"))
            if not -30 <= v <= -4:
                raise PulsarError("loudness is integrated LUFS; use something between -16 and -6", loc)
            song.loudness = v
        elif what == "ceiling":
            song.ceiling = float(num(v, loc, "ceiling"))

    def s_track_stmt(self, node, env):
        name, expr = node.children
        name = str(name)
        loc = loc_of(node)
        if self.section is not None:
            raise PulsarError("tracks must be declared at the top level, not inside a section", loc)
        inst = self.eval(expr, env)
        if isinstance(inst, Effect):
            raise PulsarError("a track needs an instrument first, e.g. synth(\"pad\") >> reverb()", loc)
        if not isinstance(inst, Instrument):
            raise PulsarError(f"track {name} = ... needs an instrument like synth(\"bass\") or drums(\"909\"), "
                              f"got {type_name(inst)}", loc)
        if name in ("master", "reverb", "delay"):
            raise PulsarError(f"{name!r} is reserved; pick another track name", loc)
        tr = Track(name, inst, dict(inst.mixer), loc)
        self.song.tracks[name] = tr
        env.define(name, tr)

    def s_bus_stmt(self, node, env):
        name, expr = node.children
        v = self.eval(expr, env)
        chain = v if isinstance(v, list) else [v]
        if not chain or not all(isinstance(x, Effect) for x in chain):
            raise PulsarError("a bus is an effect chain, e.g. bus reverb = reverb(decay: 4)", loc_of(node))
        for e in chain:
            e.params.setdefault("mix", 1.0)
        self.song.buses[str(name)] = chain

    def s_master_stmt(self, node, env):
        v = self.eval(node.children[0], env)
        chain = v if isinstance(v, list) else [v]
        if not all(isinstance(x, Effect) for x in chain):
            raise PulsarError("master = ... takes effects, e.g. master = eq(high: 1) >> comp(ratio: 2)",
                              loc_of(node))
        self.song.master = chain

    def s_section_stmt(self, node, env):
        name, bars_expr, parent, block = node.children
        name = str(name)
        loc = loc_of(node)
        if self.section is not None:
            raise PulsarError("sections can't be nested", loc)
        if env.has(name) and not isinstance(env.get(name), Section):
            raise PulsarError(f"section {name!r} would hide the {type_name(env.get(name))} named {name!r}", loc,
                              f"rename one of them, e.g. section {name}_part or let {name}_pattern")
        base = None
        if parent is not None:
            base = self.song.sections.get(str(parent))
            if base is None:
                raise PulsarError(f"unknown section {str(parent)!r} to extend", loc_of(parent),
                                  suggest(str(parent), self.song.sections))
        if bars_expr is not None:
            bars = P.to_frac(num(self.eval(bars_expr, env), loc_of(bars_expr), "section length"))
        else:
            bars = base.bars if base else Fraction(8)
        if bars <= 0:
            raise PulsarError("a section needs a positive number of bars", loc)
        sec = Section(name, bars, loc=loc)
        if base:
            sec.plays = [dc_replace(pl, inherited=True) for pl in base.plays]
            sec.autos = list(base.autos)
            sec.key, sec.swing = base.key, base.swing
        self.section = sec
        try:
            self.exec_block(block.children, Env(env))
        finally:
            self.section = None
        self.song.sections[name] = sec
        env.define(name, sec)

    def _range_opts(self, opts, env):
        start, end = Fraction(0), None
        for o in opts:
            v = P.to_frac(num(self.eval(o.children[0], env), loc_of(o), "bar position"))
            if o.data == "opt_from":
                start = v
            elif o.data == "opt_to":
                end = v
            elif o.data == "opt_at":
                start, end = v, v + 1 if end is None else end
        return start, end

    def need_section(self, what, loc):
        if self.section is None:
            raise PulsarError(f"`{what}` only works inside a section block, e.g. section intro 8 {{ {what} ... }}",
                              loc)
        return self.section

    def track(self, name, loc):
        tr = self.song.tracks.get(name)
        if tr is None:
            raise PulsarError(f"unknown track {name!r}", loc, suggest(name, self.song.tracks) or
                              "declare it first: track name = synth(\"pad\")")
        return tr

    def s_play_stmt(self, node, env):
        name, expr, *opts = node.children
        loc = loc_of(node)
        sec = self.need_section("play", loc)
        self.track(str(name), loc_of(name))
        pat = to_pattern(self.eval(expr, env), loc_of(expr))
        start, end = self._range_opts(opts, env)
        # In a section that extends another, the first play of a track replaces
        # what it inherited for that track; further plays in the same section add.
        replaced = [p for p in sec.plays if p.inherited and p.track == str(name)]
        sec.plays = [p for p in sec.plays if not (p.inherited and p.track == str(name))]
        sec.plays.append(Play(str(name), pat, start, end, loc_of(expr)))
        if replaced:
            sec.replaced = getattr(sec, "replaced", []) + [(str(name), replaced, sec.plays[-1])]

    def s_mute_stmt(self, node, env):
        sec = self.need_section("mute", loc_of(node))
        names = [str(n) for n in node.children]
        for n in names:
            self.track(n, loc_of(node))
        sec.plays = [p for p in sec.plays if p.track not in names]
        sec.autos = [a for a in sec.autos if a.track not in names]

    def s_automate_stmt(self, node, env):
        target, expr, *opts = node.children
        loc = loc_of(node)
        sec = self.need_section("automate", loc)
        if target.data == "master_target":
            tname, path = "master", [str(target.children[0])]
            if path[0] not in ("glue", "low", "high", "air", "gain"):
                raise PulsarError(f"master can automate glue, low, high, air or gain, not {path[0]!r}", loc)
        else:
            parts = [str(t) for t in target.children]
            tname, path = parts[0], parts[1:]
            tr = self.track(tname, loc)
            path = self.resolve_param_path(tr, path, loc)
        value = self.eval(expr, env)
        if isinstance(value, str):
            value = to_pattern(value, loc_of(expr))
        if not isinstance(value, (int, float, Ramp, Lfo, P.Pattern)) or isinstance(value, bool):
            raise PulsarError("automate takes a number, a ramp (0.2 -> 0.8), an lfo(...) or a value pattern "
                              "like \"0.2 0.5 0.8\"", loc_of(expr))
        start, end = self._range_opts(opts, env)
        sec.autos.append(Automation(tname, path, value, start, end, loc))

    def resolve_param_path(self, tr: Track, path: list, loc):
        inst = tr.inst
        if len(path) == 1:
            p = path[0]
            if p in ("gain", "pan") or p in ("reverb", "delay") or p in self.song.buses:
                return ["mixer", p]
            if inst.kind == "synth" and p in ENGINE_PARAMS[inst.engine]:
                return ["synth", p]
            hits = [i for i, e in enumerate(inst.inserts) if p in FX.EFFECTS[e.name][1]]
            if len(hits) == 1:
                return ["fx", hits[0], p]
            if len(hits) > 1:
                raise PulsarError(f"{p!r} is on several effects of {tr.name}; say which, e.g. "
                                  f"{tr.name}.{inst.inserts[hits[0]].name}.{p}", loc)
            opts = ["gain", "pan"] + (list(ENGINE_PARAMS.get(inst.engine, {})) if inst.kind == "synth" else [])
            raise PulsarError(f"{tr.name} has no parameter {p!r}", loc, suggest(p, opts))
        if len(path) == 2:
            ename, p = path
            base, _, nth = ename.rpartition("_")
            want = 1
            if base and nth.isdigit():
                ename, want = base, int(nth)
            seen = 0
            for i, e in enumerate(inst.inserts):
                if e.name == ename:
                    seen += 1
                    if seen != want:
                        continue
                    if p not in FX.EFFECTS[e.name][1]:
                        raise PulsarError(f"effect {ename} has no parameter {p!r}", loc,
                                          suggest(p, FX.EFFECTS[e.name][1]))
                    return ["fx", i, p]
            raise PulsarError(f"{tr.name} has no {ename!r} effect", loc,
                              suggest(ename, [e.name for e in inst.inserts]))
        raise PulsarError("automation targets look like track.param or track.effect.param", loc)

    def s_arrange_stmt(self, node, env):
        for expr in node.children:
            v = self.eval(expr, env)
            items = v if isinstance(v, list) else [v]
            for s in items:
                if not isinstance(s, Section):
                    raise PulsarError(f"arrange takes sections, got {type_name(s)}", loc_of(expr))
                self.song.arrangement.append(s)

    def set_track_attr(self, tr: Track, attr, value, loc):
        if attr in presets.MIXER_KEYS or attr in self.song.buses:
            if attr == "duck_by":
                tr.mixer[attr] = value
            else:
                tr.mixer[attr] = float(num(value, loc, attr))
            return
        if tr.inst.kind == "synth" and attr in ENGINE_PARAMS[tr.inst.engine]:
            tr.inst.params[attr] = float(num(value, loc, attr))
            return
        raise PulsarError(f"can't set {tr.name}.{attr}", loc,
                          suggest(attr, list(presets.MIXER_KEYS) + list(ENGINE_PARAMS.get(tr.inst.engine, {}))))

    # -- expressions -----------------------------------------------------------

    def eval(self, node, env):
        if isinstance(node, Token):
            raise PulsarError(f"unexpected {node!s}", loc_of(node))
        fn = getattr(self, f"e_{node.data}", None)
        if fn is None:
            raise PulsarError(f"internal: no evaluator for {node.data}", loc_of(node))
        return fn(node, env)

    def e_number(self, node, env):
        t = str(node.children[0])
        if "." in t or "e" in t.lower():
            return float(t)
        return int(t)

    def e_string(self, node, env):
        raw = str(node.children[0])
        body = raw[1:-1]
        if raw[0] != "`":
            body = body.encode().decode("unicode_escape")
        s = PStr(body)
        s.loc = loc_of(node)
        return s

    def e_true(self, node, env):
        return True

    def e_false(self, node, env):
        return False

    def e_none(self, node, env):
        return None

    def e_name(self, node, env):
        name = str(node.children[0])
        try:
            return env.get(name)
        except KeyError:
            raise PulsarError(f"unknown name {name!r}", loc_of(node), suggest(name, env.names())) from None

    def e_list(self, node, env):
        return [self.eval(c, env) for c in node.children if c is not None]

    def e_neg(self, node, env):
        op, v = node.children
        v = self.eval(v, env)
        return -num(v, loc_of(node)) if str(op) == "-" else v

    def e_not_op(self, node, env):
        return not self.truthy(self.eval(node.children[0], env))

    def e_cond(self, node, env):
        a, c, b = node.children
        return self.eval(a, env) if self.truthy(self.eval(c, env)) else self.eval(b, env)

    def e_or_expr(self, node, env):
        v = None
        for c in node.children:
            v = self.eval(c, env)
            if self.truthy(v):
                return v
        return v

    def e_and_expr(self, node, env):
        v = None
        for c in node.children:
            v = self.eval(c, env)
            if not self.truthy(v):
                return v
        return v

    def truthy(self, v):
        if isinstance(v, (P.Pattern, Instrument, Effect, Section, Track)):
            return True
        return bool(v)

    def e_compare(self, node, env):
        items = node.children
        left = self.eval(items[0], env)
        for i in range(1, len(items), 2):
            op = str(items[i])
            right = self.eval(items[i + 1], env)
            try:
                ok = {"==": left == right, "!=": left != right}.get(op)
                if ok is None:
                    ok = {"<": left < right, ">": left > right, "<=": left <= right, ">=": left >= right}[op]
            except TypeError:
                raise PulsarError(f"can't compare {type_name(left)} and {type_name(right)}", loc_of(node)) from None
            if not ok:
                return False
            left = right
        return True

    def e_chain(self, node, env):
        items = [c for c in node.children if not isinstance(c, Token)]
        acc = self.eval(items[0], env)
        for c in items[1:]:
            nxt = self.eval(c, env)
            acc = self.chain(acc, nxt, loc_of(c))
        return acc

    def chain(self, a, b, loc):
        if isinstance(b, list):
            for x in b:
                a = self.chain(a, x, loc)
            return a
        if not isinstance(b, Effect):
            raise PulsarError(f"only effects can follow >>, got {type_name(b)}", loc)
        if isinstance(a, Instrument):
            return a.chained(b)
        if isinstance(a, Effect):
            return [a, b]
        if isinstance(a, list) and all(isinstance(x, Effect) for x in a):
            return a + [b]
        raise PulsarError(f"can't chain an effect after {type_name(a)}", loc)

    def e_ramp(self, node, env):
        a, _, b = node.children
        return Ramp(float(num(self.eval(a, env), loc_of(a))), float(num(self.eval(b, env), loc_of(b))))

    def e_range(self, node, env):
        a, _, b = node.children
        lo, hi = num(self.eval(a, env), loc_of(a)), num(self.eval(b, env), loc_of(b))
        return list(range(int(lo), int(hi)))

    def e_sum(self, node, env):
        return self._arith(node, env)

    def e_product(self, node, env):
        return self._arith(node, env)

    def _arith(self, node, env):
        items = node.children
        acc = self.eval(items[0], env)
        for i in range(1, len(items), 2):
            op = str(items[i])
            rhs = self.eval(items[i + 1], env)
            acc = self.binop(op, acc, rhs, loc_of(items[i]))
        return acc

    def binop(self, op, a, b, loc):
        if op == "*" and isinstance(a, Section) and isinstance(b, int):
            return [a] * b
        if op == "*" and isinstance(a, list) and isinstance(b, int):
            return a * b
        if op == "+" and isinstance(a, list) and isinstance(b, list):
            return a + b
        if op == "+" and isinstance(a, str) and isinstance(b, str):
            s = PStr(a + " " + b if op == "+" else a)
            s.loc = getattr(a, "loc", None)
            return s
        if op == "+" and isinstance(a, list) and isinstance(b, Section):
            return a + [b]
        if op == "+" and isinstance(a, Section) and isinstance(b, (Section, list)):
            return [a] + (b if isinstance(b, list) else [b])
        num(a, loc, f"the left side of {op}")
        num(b, loc, f"the right side of {op}")
        try:
            if op == "+":
                return a + b
            if op == "-":
                return a - b
            if op == "*":
                return a * b
            if op == "/":
                if b == 0:
                    raise PulsarError("division by zero", loc)
                r = a / b
                return int(r) if isinstance(a, int) and isinstance(b, int) and a % b == 0 else r
            if op == "%":
                return a % b
        except ZeroDivisionError:
            raise PulsarError("division by zero", loc) from None
        raise PulsarError(f"unknown operator {op}", loc)

    def e_index(self, node, env):
        obj, idx = node.children
        o, i = self.eval(obj, env), self.eval(idx, env)
        if isinstance(o, (list, str)) and isinstance(i, int):
            if not -len(o) <= i < len(o):
                raise PulsarError(f"index {i} is out of range (length {len(o)})", loc_of(node))
            return o[i]
        raise PulsarError(f"can't index {type_name(o)} with {type_name(i)}", loc_of(node))

    def e_getattr(self, node, env):
        obj_node, attr = node.children
        obj = self.eval(obj_node, env)
        attr = str(attr)
        loc = loc_of(node)
        if isinstance(obj, Track):
            if attr in obj.mixer:
                return obj.mixer[attr]
            if attr in obj.inst.params:
                return obj.inst.params[attr]
            raise PulsarError(f"{obj.name}.{attr} isn't set", loc)
        if isinstance(obj, Section):
            if attr == "bars":
                return obj.bars
            raise PulsarError(f"sections only have .bars", loc)
        if isinstance(obj, (str, P.Pattern)):
            m = PATTERN_METHODS.get(attr)
            if m is None:
                raise PulsarError(f"patterns have no method .{attr}()", loc, suggest(attr, PATTERN_METHODS))
            pat = to_pattern(obj, loc)
            return Bound(m, pat, attr)
        if isinstance(obj, list):
            if attr == "len":
                return len(obj)
        raise PulsarError(f"{type_name(obj)} has no .{attr}", loc)

    def e_lambda_expr(self, node, env):
        params, _, body = node.children
        return Function("lambda", self.params(params, env), body, env, self, True)

    def e_call(self, node, env):
        fn_node, args_node = node.children
        loc = loc_of(node)
        fn = self.eval(fn_node, env)
        args, kwargs = [], {}
        if args_node is not None:
            for a in args_node.children:
                if isinstance(a, Tree) and a.data == "kwarg":
                    k, v = a.children
                    if str(k) in kwargs:
                        raise PulsarError(f"argument {str(k)!r} given twice", loc_of(a))
                    kwargs[str(k)] = self.eval(v, env)
                else:
                    if kwargs:
                        raise PulsarError("positional arguments must come before named ones", loc_of(a))
                    args.append(self.eval(a, env))
        return self.call(fn, args, kwargs, loc)

    def call(self, fn, args, kwargs, loc):
        if isinstance(fn, Function):
            return self.call_user(fn, args, kwargs, loc)
        if isinstance(fn, Bound):
            return fn.call(self, args, kwargs, loc)
        if isinstance(fn, Builtin):
            return fn.call(self, args, kwargs, loc)
        raise PulsarError(f"{type_name(fn)} is not callable", loc)

    def call_user(self, fn: Function, args, kwargs, loc):
        if len(args) > len(fn.params):
            raise PulsarError(f"{fn.name}() takes {len(fn.params)} arguments, got {len(args)}", loc)
        inner = Env(fn.env)
        names = [p for p, _ in fn.params]
        for k in kwargs:
            if k not in names:
                raise PulsarError(f"{fn.name}() has no parameter {k!r}", loc, suggest(k, names))
        for i, (pname, default) in enumerate(fn.params):
            if i < len(args):
                inner.define(pname, args[i])
            elif pname in kwargs:
                inner.define(pname, kwargs[pname])
            elif default is not None:
                inner.define(pname, self.eval(default, fn.env))
            else:
                raise PulsarError(f"{fn.name}() is missing argument {pname!r}", loc)
        self.depth += 1
        if self.depth > 200:
            raise PulsarError(f"too much recursion in {fn.name}()", loc)
        try:
            if fn.is_expr:
                return self.eval(fn.body, inner)
            try:
                self.exec_block(fn.body.children, inner)
            except Return as r:
                return r.value
            return None
        finally:
            self.depth -= 1

    # -- builtins ----------------------------------------------------------------

    def _install_builtins(self):
        g = self.globals
        for name, f in BUILTINS.items():
            g.define(name, Builtin(name, f))
        for name in FX.EFFECTS:
            g.define(name, Builtin(name, make_effect_builder(name)))
        for alias, real in FX.FX_ALIASES.items():
            g.define(alias, Builtin(alias, make_effect_builder(real)))
        g.define("pi", math.pi)
        self.song.buses = {"reverb": [Effect("reverb", {"mix": 1.0, "decay": 2.8, "size": 0.7})],
                           "delay": [Effect("delay", {"mix": 1.0, "time": Fraction(3, 16), "feedback": 0.4})]}


class Builtin:
    def __init__(self, name, fn):
        self.name, self.fn = name, fn

    def call(self, interp, args, kwargs, loc):
        return invoke(self.fn, self.name, [interp] + list(args), kwargs, loc, skip=1)

    def __repr__(self):
        return f"<builtin {self.name}>"


class Bound:
    def __init__(self, fn, pat, name):
        self.fn, self.pat, self.name = fn, pat, name

    def call(self, interp, args, kwargs, loc):
        return invoke(self.fn, "." + self.name, [interp, self.pat] + list(args), kwargs, loc, skip=2)


def invoke(fn, name, args, kwargs, loc, skip=0):
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    has_var_kw = any(p.kind == p.VAR_KEYWORD for p in params)
    has_var_pos = any(p.kind == p.VAR_POSITIONAL for p in params)
    named = [p.name for p in params if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)]
    if not has_var_kw:
        for k in kwargs:
            if k not in named[skip:]:
                raise PulsarError(f"{name}() has no parameter {k!r}", loc, suggest(k, named[skip:]) or
                                  (f"parameters: {', '.join(named[skip:])}" if named[skip:] else None))
    try:
        sig.bind(*args, **kwargs)
    except TypeError as e:
        visible = ", ".join(named[skip:])
        raise PulsarError(f"wrong arguments for {name}({visible}): {e}", loc) from None
    try:
        return fn(*args, **kwargs)
    except PulsarError as e:
        if e.loc is None:
            e.loc = loc
        raise
    except (TypeError, ValueError, ZeroDivisionError) as e:
        raise PulsarError(f"{name}(): {e}", loc) from None


# -- instruments -----------------------------------------------------------------

def _split_kwargs(engine, kw, buses, where):
    params, mixer, shape = {}, {}, {}
    allowed = ENGINE_PARAMS[engine]
    for k, v in kw.items():
        if k in SHAPE_KEYS:
            shape[k] = v
        elif k in presets.MIXER_KEYS or k in buses:
            mixer[k] = v
        elif k in SYNC_KEYS:
            params[k] = v  # converted to Hz at render time
        elif k in allowed:
            params[k] = float(num(v, None, k))
            lo, hi = allowed[k][1], allowed[k][2]
            if not lo <= params[k] <= hi:
                raise PulsarError(f"{where}: {k} = {v} is out of range [{lo}, {hi}]")
        else:
            raise PulsarError(f"{where} has no parameter {k!r}",
                              hint=suggest(k, list(allowed) + list(presets.MIXER_KEYS) + list(SHAPE_KEYS)))
    return params, mixer, shape


def _validate_shape(shape, where):
    if "wave1" in shape and shape["wave1"] not in FZ.WAVES:
        raise PulsarError(f"{where}: wave1 must be one of {', '.join(sorted(FZ.WAVES))}")
    if "wave2" in shape and shape["wave2"] not in FZ.WAVES:
        raise PulsarError(f"{where}: wave2 must be one of {', '.join(sorted(FZ.WAVES))}")
    if "filter" in shape and shape["filter"] not in FZ.FILTERS:
        raise PulsarError(f"{where}: filter must be one of {', '.join(sorted(FZ.FILTERS))}")
    if "unison" in shape and not 1 <= int(shape["unison"]) <= 9:
        raise PulsarError(f"{where}: unison goes from 1 to 9 voices")


def b_synth(interp, preset="pad", **kw):
    pre = presets.SYNTHS.get(str(preset))
    if pre is None:
        raise PulsarError(f"unknown synth preset {preset!r}", hint=suggest(str(preset), presets.SYNTHS) or
                          f"presets: {', '.join(sorted(presets.SYNTHS))}")
    engine = pre["engine"]
    params, mixer, shape = _split_kwargs(engine, kw, interp.song.buses, f'synth("{preset}")')
    _validate_shape(shape, f'synth("{preset}")')
    base_shape = {k: pre[k] for k in ("wave1", "wave2", "unison", "filter") if k in pre}
    base_shape.update({k: v for k, v in shape.items() if k != "mono"})
    mono = bool(shape.get("mono", pre.get("mono", False)))
    octave = int(mixer.pop("octave", pre.get("octave", 4)))
    mix = {"gain": 0.0}
    mix.update(mixer)
    return Instrument("synth", str(preset), engine, base_shape, {**pre["params"], **params}, mono, octave, mix,
                      base_gain=float(pre.get("gain", -8)))


def _engine_builder(engine, default_gain):
    def build(interp, **kw):
        params, mixer, shape = _split_kwargs(engine, kw, interp.song.buses, f"{engine}()")
        _validate_shape(shape, f"{engine}()")
        sh = {"wave1": "saw", "wave2": "saw", "unison": 1, "filter": "ladder"} if engine == "analog" else {}
        sh.update({k: v for k, v in shape.items() if k != "mono"})
        octave = int(mixer.pop("octave", 4))
        mix = {"gain": 0.0}
        mix.update(mixer)
        return Instrument("synth", engine, engine, sh, params, bool(shape.get("mono", False)), octave, mix,
                          base_gain=default_gain)
    return build


SAMPLER_KEYS = {"root", "start", "end", "slices", "attack", "release", "gate", "speed", "reverse", "fit", "transpose"}


def b_sampler(interp, path, **kw):
    """Play an audio file. Steps: note names (pitched from `root`), integers
    (slice numbers when `slices` is set) or any other word (play as recorded)."""
    from pathlib import Path as _P
    p = _P(str(path)).expanduser()
    if not p.is_absolute():
        p = (interp.basedir / p).resolve()
    if not p.exists():
        near = sorted(x.name for x in p.parent.glob("*")) if p.parent.exists() else []
        raise PulsarError(f"sample file not found: {p}", hint=suggest(p.name, near) or
                          "paths are relative to the song file")
    opts = {"root": 60, "start": 0.0, "end": 1.0, "slices": 0, "attack": 0.003, "release": 0.05,
            "gate": False, "speed": 1.0, "reverse": False, "fit": False, "transpose": 0.0}
    mixer = {}
    for k, v in kw.items():
        if k in presets.MIXER_KEYS or k in interp.song.buses:
            mixer[k] = v
        elif k == "root":
            n = theory.note_number(str(v)) if isinstance(v, str) else int(num(v, None, k))
            if n is None:
                raise PulsarError(f"sampler root must be a note name like a4 (the pitch the sample was recorded at)")
            opts["root"] = n
        elif k in ("gate", "reverse", "fit"):
            if not isinstance(v, bool):
                raise PulsarError(f"sampler {k} takes true or false")
            opts[k] = v
        elif k in SAMPLER_KEYS:
            opts[k] = float(num(v, None, k))
        else:
            raise PulsarError(f"sampler() has no parameter {k!r}",
                              hint=suggest(k, list(SAMPLER_KEYS) + list(presets.MIXER_KEYS)))
    if not 0 <= opts["start"] < opts["end"] <= 1:
        raise PulsarError("sampler start/end are fractions of the file with 0 <= start < end <= 1")
    if opts["speed"] <= 0:
        raise PulsarError("sampler speed must be positive (use reverse: true to play backwards)")
    opts["slices"] = int(opts["slices"])
    opts["path"] = str(p)
    octave = int(mixer.pop("octave", 4))
    mix = {"gain": 0.0}
    mix.update(mixer)
    return Instrument("sampler", p.name, "sampler", {}, {}, False, octave, mix, base_gain=-6.0, sample=opts)


VOICE_KEYS = {"lyrics", "mode", "speed", "inflection", "vibrato", "vib_rate", "formant", "double", "lang", "transpose"}


def b_voice(interp, name="af_heart", **kw):
    """A neural text-to-speech voice that sings (one lyric word per note) or speaks (one line per hit)."""
    opts = {"voice": str(name), "lyrics": "", "mode": "sung", "speed": 1.0, "inflection": 0.15, "vibrato": 0.35,
            "vib_rate": 5.4, "formant": 0.0, "double": 0.0, "lang": "en-us", "transpose": 0.0}
    mixer = {}
    for k, v in kw.items():
        if k in presets.MIXER_KEYS or k in interp.song.buses:
            mixer[k] = v
        elif k in ("lyrics", "mode", "lang"):
            if not isinstance(v, str):
                raise PulsarError(f"voice {k} takes a string")
            opts[k] = str(v)
        elif k in VOICE_KEYS:
            opts[k] = float(num(v, None, k))
        else:
            raise PulsarError(f"voice() has no parameter {k!r}", hint=suggest(k, list(VOICE_KEYS) + list(presets.MIXER_KEYS)))
    if opts["mode"] not in ("sung", "speak"):
        raise PulsarError('voice mode must be "sung" or "speak"')
    if not opts["lyrics"].strip():
        raise PulsarError('voice() needs lyrics: voice("af_heart", lyrics: "words to sing")')
    # Voices sing best near their speaking range: octave 3 puts degree 0 on A3 for A minor.
    octave = int(mixer.pop("octave", 3))
    mix = {"gain": 0.0}
    mix.update(mixer)
    return Instrument("voice", str(name), "voice", {}, {}, False, octave, mix, base_gain=-5.0, sample=opts)


def b_drums(interp, kit="909", **kw):
    k = presets.KITS.get(str(kit))
    if k is None:
        raise PulsarError(f"unknown drum kit {kit!r}", hint=suggest(str(kit), presets.KITS) or
                          f"kits: {', '.join(sorted(presets.KITS))}")
    voices = {v: dict(k.get(v, {})) for v in FZ.DRUM_NAMES}
    levels = dict(presets.DRUM_LEVELS)
    levels.update(k.get("levels", {}))
    mixer = {"gain": 0.0}
    for key, v in kw.items():
        if key in presets.MIXER_KEYS or key in interp.song.buses:
            mixer[key] = v
            continue
        voice, _, param = key.partition("_")
        voice = P.DRUM_ALIASES.get(voice, voice)
        if voice not in FZ.DRUM_PARAMS or not param:
            raise PulsarError(f"drums() has no parameter {key!r}; use voice_param like bd_decay, hh_tune, sd_gain",
                              hint=suggest(key, [f"{d}_{p}" for d in FZ.DRUM_PARAMS for p in FZ.DRUM_PARAMS[d]]))
        if param == "gain":
            levels[voice] = levels.get(voice, 0) + float(num(v, None, key))
            continue
        if param not in FZ.DRUM_PARAMS[voice]:
            raise PulsarError(f"drum voice {voice} has no parameter {param!r}",
                              hint=suggest(key, [f"{voice}_{p}" for p in list(FZ.DRUM_PARAMS[voice]) + ["gain"]]))
        voices[voice][param] = float(num(v, None, key))
    return Instrument("drums", str(kit), "drums", {}, {}, False, 4, mixer, kit=voices, levels=levels)


def make_effect_builder(name):
    params_spec = FX.EFFECTS[name][1]
    first = EFFECT_POSITIONAL.get(name)

    def build(interp, *args, **kw):
        if len(args) > 1:
            raise PulsarError(f"{name}() takes at most one positional argument ({first}); name the rest")
        if args:
            kw = {first: args[0], **kw}
        out = {}
        for k, v in kw.items():
            if name == "delay" and k == "ms":
                out["time_ms"] = float(num(v, None, k))
                continue
            if name == "tremolo" and k == "sync":
                out["sync"] = float(num(v, None, k))
                continue
            if k not in params_spec:
                raise PulsarError(f"{name}() has no parameter {k!r}", hint=suggest(k, params_spec) or
                                  f"parameters: {', '.join(params_spec)}")
            out[k] = P.to_frac(v) if (name == "delay" and k == "time") else float(num(v, None, k))
            if name != "delay" or k != "time":
                lo, hi = params_spec[k][1], params_spec[k][2]
                if not lo <= out[k] <= hi:
                    raise PulsarError(f"{name}(): {k} = {v} is out of range [{lo}, {hi}]")
        return Effect(name, out)

    return build


# -- functions ---------------------------------------------------------------------

def b_seq(interp, *pats):
    return P.fastcat([to_pattern(p) for p in pats])


def b_cat(interp, *pats):
    return P.slowcat([to_pattern(p) for p in pats])


def b_stack(interp, *pats):
    return P.stack([to_pattern(p) for p in pats])


def b_euclid(interp, hits, steps, rotate=0, pattern="x"):
    return P.euclid(int(hits), int(steps), int(rotate), to_pattern(pattern))


def b_ramp(interp, start, end, curve="lin"):
    if curve not in ("lin", "exp", "smooth", "in", "out"):
        raise PulsarError("ramp curve must be lin, exp, smooth, in or out")
    return Ramp(float(start), float(end), str(curve))


def b_lfo(interp, period=1, min=0.0, max=1.0, shape="sine", phase=0.0):
    if float(period) <= 0:
        raise PulsarError("lfo period must be positive (bars per cycle, e.g. 1/4 for once per beat)")
    return Lfo(float(period), float(min), float(max), str(shape), float(phase))


def b_print(interp, *vals):
    line = " ".join(str(v) for v in vals)
    interp.output.append(line)
    return None


def b_range(interp, a, b=None, step=1):
    if b is None:
        a, b = 0, a
    return list(range(int(a), int(b), int(step)))


def b_len(interp, x):
    return len(x)


_rand_counter = [0]


def b_rand(interp, lo=0.0, hi=1.0):
    _rand_counter[0] += 1
    return float(lo) + (float(hi) - float(lo)) * P.rand(interp.song.seed, 77, _rand_counter[0])


def b_choose(interp, options):
    if not isinstance(options, list) or not options:
        raise PulsarError("choose() takes a non-empty list")
    _rand_counter[0] += 1
    return options[int(P.rand(interp.song.seed, 91, _rand_counter[0]) * len(options))]


def b_note(interp, name):
    n = theory.note_number(str(name))
    if n is None:
        raise PulsarError(f"{name!r} is not a note name like a3 or eb2")
    return n


def b_chord(interp, symbol, octave=4):
    ch = theory.chord_symbol(str(symbol), int(octave))
    if ch is None:
        raise PulsarError(f"{symbol!r} is not a chord symbol like Am7 or Fmaj9")
    return P.pure({"notes": ch})


BUILTINS = {
    "synth": b_synth, "analog": _engine_builder("analog", -9.0), "fm": _engine_builder("fm", -9.0),
    "pluck": _engine_builder("pluck", -9.0), "drums": b_drums, "sampler": b_sampler, "voice": b_voice,
    "seq": b_seq, "cat": b_cat, "stack": b_stack, "euclid": b_euclid,
    "ramp": b_ramp, "lfo": b_lfo, "print": b_print, "range": b_range, "len": b_len,
    "rand": b_rand, "choose": b_choose, "note": b_note, "chord": b_chord,
    "min": lambda interp, *a: min(*a), "max": lambda interp, *a: max(*a), "abs": lambda interp, x: abs(x),
    "round": lambda interp, x, n=0: round(x, int(n)) if n else int(round(x)),
    "floor": lambda interp, x: math.floor(x), "ceil": lambda interp, x: math.ceil(x),
    "silence": lambda interp: P.silence(),
}


# -- pattern methods -----------------------------------------------------------------

def _fnarg(interp, f, what):
    if isinstance(f, (Function, Builtin, Bound)):
        def call(p):
            r = interp.call(f, [p], {}, None)
            return to_pattern(r, None, f"result of the function passed to {what}")
        return call
    raise PulsarError(f"{what} takes a function, e.g. fn(p) => p.fast(2)")


def _up(interp, pat, steps):
    """Diatonic transpose: move each note `steps` scale degrees within the key."""
    steps = int(steps)

    def q(b, e, c):
        out = []
        sc = list(c.scale)
        for ev in pat.q(b, e, c):
            if "notes" not in ev.v:
                out.append(ev)
                continue
            new = []
            for n in ev.v["notes"]:
                rel = (n - c.key_pc) % 12
                octs = (n - c.key_pc - rel) // 12
                idx = max(i for i, s in enumerate(sc) if s <= rel)
                chroma = rel - sc[idx]
                o2, i2 = divmod(idx + steps, len(sc))
                new.append(c.key_pc + 12 * (octs + o2) + sc[i2] + chroma)
            out.append(P.Ev(ev.b, ev.e, {**ev.v, "notes": new}))
        return out

    return P.Pattern(q)


VOICINGS = ("close", "open", "drop2", "spread", "smooth")


def _voice(notes, mode, c):
    notes = sorted(notes)
    if len(notes) < 2 or mode == "close":
        return notes
    if mode == "open":  # lift the second note an octave: wider, airier triads
        return sorted([notes[0]] + [notes[1] + 12] + notes[2:])
    if mode == "drop2":  # drop the second-highest note an octave (jazz/house voicing)
        return sorted(notes[:-2] + [notes[-2] - 12] + notes[-1:])
    if mode == "spread":  # root an octave down under the rest
        return [notes[0] - 12] + notes[1:]
    if mode == "smooth":  # the inversion that sits closest to the middle of the track's octave
        center = 12 * (c.octave + 1) + 7
        best = None
        for k in range(len(notes)):
            inv = notes[k:] + [n + 12 for n in notes[:k]]
            for o in (-12, 0, 12):
                cand = [n + o for n in inv]
                score = abs(sum(cand) / len(cand) - center)
                if best is None or score < best[0] - 1e-9:
                    best = (score, cand)
        return best[1]
    raise PulsarError(f"unknown voicing {mode!r}; use {', '.join(VOICINGS)}")


def _voicing(interp, pat, mode="smooth"):
    mode = str(mode)
    if mode not in VOICINGS:
        raise PulsarError(f"unknown voicing {mode!r}; use {', '.join(VOICINGS)}")

    def q(b, e, c):
        return [P.Ev(ev.b, ev.e, {**ev.v, "notes": _voice(ev.v["notes"], mode, c)}) if "notes" in ev.v else ev
                for ev in pat.q(b, e, c)]
    return P.Pattern(q)


def _invert(interp, pat, n=1):
    n = int(n)

    def inv(notes):
        notes = sorted(notes)
        for _ in range(abs(n)):
            notes = notes[1:] + [notes[0] + 12] if n > 0 else [notes[-1] - 12] + notes[:-1]
        return notes
    return pat.fmap(lambda v: {**v, "notes": inv(v["notes"])} if v.get("notes") else v)


def _key(interp, pat, key):
    try:
        pc, scale = theory.parse_key(str(key))
    except ValueError as e:
        raise PulsarError(str(e)) from None
    return pat.with_ctx(lambda c: dc_replace(c, key_pc=pc, scale=tuple(scale)))


def _ctrl(name, kind="num"):
    def m(interp, pat, value=None):
        if value is None:
            value = "x" if kind == "bool" else 1
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            v = float(value)
            return pat.fmap(lambda d: {**d, name: v})
        if isinstance(value, bool):
            return pat.fmap(lambda d: {**d, name: value})
        return pat.ctrl(name, to_pattern(value), kind)
    return m


def _humanize(interp, pat, ms=6, vel=0.08):
    bar = interp.song.bar_seconds
    return pat.humanize(P.to_frac(float(ms) / 1000.0 / bar), vel)


PATTERN_METHODS = {
    "fast": lambda i, p, n: p.fast(P.to_frac(n)),
    "slow": lambda i, p, n: p.slow(P.to_frac(n)),
    "rev": lambda i, p: p.rev(),
    "ply": lambda i, p, n: p.ply(n),
    "palindrome": lambda i, p: p.palindrome(),
    "iter": lambda i, p, n: p.iter(n),
    "late": lambda i, p, t: p.late(P.to_frac(t)),
    "early": lambda i, p, t: p.early(P.to_frac(t)),
    "swing": lambda i, p, amount=0.15, grid=Fraction(1, 8): p.swing(amount, P.to_frac(grid)),
    "humanize": _humanize,
    "degrade": lambda i, p, prob=0.5: p.degrade(prob, salt=17),
    "every": lambda i, p, n, f, offset=0: p.every(n, _fnarg(i, f, ".every()"), offset),
    "lastof": lambda i, p, n, f: p.lastof(n, _fnarg(i, f, ".lastof()")),
    "sometimes": lambda i, p, f, prob=0.5: p.sometimes(_fnarg(i, f, ".sometimes()"), prob),
    "often": lambda i, p, f: p.sometimes(_fnarg(i, f, ".often()"), 0.75, salt=8),
    "rarely": lambda i, p, f: p.sometimes(_fnarg(i, f, ".rarely()"), 0.25, salt=9),
    "off": lambda i, p, t, f: p.off(P.to_frac(t), _fnarg(i, f, ".off()")),
    "echo": lambda i, p, n=3, time=Fraction(1, 8), feedback=0.6: p.echo(n, P.to_frac(time), feedback),
    "struct": lambda i, p, s: p.struct(to_pattern(s)),
    "mask": lambda i, p, s: p.mask(to_pattern(s)),
    "vel": _ctrl("vel"),
    "legato": _ctrl("legato"),
    "slide": _ctrl("slide", "bool"),
    "accent": _ctrl("accent", "bool"),
    "transpose": lambda i, p, n: p.transpose(n),
    "oct": lambda i, p, n: p.transpose(12 * int(n)),
    "octave": lambda i, p, n: p.with_ctx(lambda c: dc_replace(c, octave=int(n))),
    "key": _key,
    "voicing": _voicing,
    "invert": _invert,
    "up": _up,
    "arp": lambda i, p, mode="up", rate=None, octaves=1: p.arp(str(mode), None if rate is None else P.to_frac(rate),
                                                              octaves),
    "stack": lambda i, p, *others: P.stack([p] + [to_pattern(o) for o in others]),
    "then": lambda i, p, *others: P.slowcat([p] + [to_pattern(o) for o in others]),
}


def run(source: str, filename: str = "<song>") -> tuple[Song, list]:
    it = Interp(source, filename)
    song = it.run()
    return song, it.output
