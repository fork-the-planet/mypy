"""Always defined attribute analysis.

An always defined attribute has some statements in __init__ or the
class body that cause the attribute to be always initialized when an
instance is constructed. It must also not be possible to read the
attribute before initialization, and it can't be deletable.

We can assume that the value is always defined when reading an always
defined attribute. Otherwise we'll need to raise AttributeError if the
value is undefined (i.e. has the error value).

We use data flow analysis to figure out attributes that are always
defined. Example:

  class C:
      def __init__(self) -> None:
          self.x = 0
          if func():
              self.y = 1
          else:
              self.y = 2
              self.z = 3

In this example, the attributes 'x' and 'y' are always defined, but 'z'
is not. The analysis assumes that we know that there won't be any subclasses.

The analysis also works if there is a known, closed set of subclasses.
An attribute defined in a base class can only be always defined if it's
also always defined in all subclasses.

As soon as __init__ contains an op that can 'leak' self to another
function, we will stop inferring always defined attributes, since the
analysis is mostly intra-procedural and only looks at __init__ methods.
The called code could read an uninitialized attribute. Example:

  class C:
      def __init__(self) -> None:
          self.x = self.foo()

      def foo(self) -> int:
          ...

Now we won't infer 'x' as always defined, since 'foo' might read 'x'
before initialization.

As an exception to the above limitation, we perform inter-procedural
analysis of super().__init__ calls, since these are very common.

Our analysis is somewhat optimistic. We assume that nobody calls a
method of a partially uninitialized object through gc.get_objects(), in
particular. Code like this could potentially cause a segfault with a null
pointer dereference. This seems very unlikely to be an issue in practice,
however.

Accessing an attribute via getattr always checks for undefined attributes
and thus works if the object is partially uninitialized. This can be used
as a workaround if somebody ever needs to inspect partially uninitialized
objects via gc.get_objects().

The analysis runs after IR building as a separate pass. Since we only
run this on __init__ methods, this analysis pass will be fairly quick.
"""

from __future__ import annotations

from typing import Final

from mypyc.analysis.dataflow import (
    CFG,
    MAYBE_ANALYSIS,
    AnalysisResult,
    BaseAnalysisVisitor,
    get_cfg,
    run_analysis,
)
from mypyc.analysis.selfleaks import analyze_self_leaks
from mypyc.ir.class_ir import ClassIR
from mypyc.ir.ops import (
    Assign,
    AssignMulti,
    BasicBlock,
    Branch,
    Call,
    ControlOp,
    GetAttr,
    Register,
    RegisterOp,
    Return,
    SetAttr,
    SetMem,
    Unreachable,
)
from mypyc.ir.rtypes import RInstance

# If True, print out all always-defined attributes of native classes (to aid
# debugging and testing)
dump_always_defined: Final = False


def analyze_always_defined_attrs(class_irs: list[ClassIR]) -> None:
    """Find always defined attributes all classes of a compilation unit.

    Also tag attribute initialization ops to not decref the previous
    value (as this would read a NULL pointer and segfault).

    Update the _always_initialized_attrs, _sometimes_initialized_attrs
    and init_self_leak attributes in ClassIR instances.

    This is the main entry point.
    """
    seen: set[ClassIR] = set()

    # First pass: only look at target class and classes in MRO
    for cl in class_irs:
        analyze_always_defined_attrs_in_class(cl, seen)

    # Second pass: look at all derived class
    seen = set()
    for cl in class_irs:
        update_always_defined_attrs_using_subclasses(cl, seen)

    # Final pass: detect attributes that need to use a bitmap to track definedness
    seen = set()
    for cl in class_irs:
        detect_undefined_bitmap(cl, seen)


def analyze_always_defined_attrs_in_class(cl: ClassIR, seen: set[ClassIR]) -> None:
    if cl in seen:
        return

    seen.add(cl)

    if (
        cl.is_trait
        or cl.inherits_python
        or cl.allow_interpreted_subclasses
        or cl.builtin_base is not None
        or cl.children is None
        or cl.is_serializable()
    ):
        # Give up -- we can't enforce that attributes are always defined.
        return

    # First analyze all base classes. Track seen classes to avoid duplicate work.
    for base in cl.mro[1:]:
        analyze_always_defined_attrs_in_class(base, seen)

    m = cl.get_method("__init__")
    if m is None:
        cl._always_initialized_attrs = cl.attrs_with_defaults.copy()
        cl._sometimes_initialized_attrs = cl.attrs_with_defaults.copy()
        return
    self_reg = m.arg_regs[0]
    cfg = get_cfg(m.blocks)
    dirty = analyze_self_leaks(m.blocks, self_reg, cfg)
    maybe_defined = analyze_maybe_defined_attrs_in_init(
        m.blocks, self_reg, cl.attrs_with_defaults, cfg
    )
    all_attrs: set[str] = set()
    for base in cl.mro:
        all_attrs.update(base.attributes)
    maybe_undefined = analyze_maybe_undefined_attrs_in_init(
        m.blocks, self_reg, initial_undefined=all_attrs - cl.attrs_with_defaults, cfg=cfg
    )

    always_defined = find_always_defined_attributes(
        m.blocks, self_reg, all_attrs, maybe_defined, maybe_undefined, dirty
    )
    always_defined = {a for a in always_defined if not cl.is_deletable(a)}

    cl._always_initialized_attrs = always_defined
    if dump_always_defined:
        print(cl.name, sorted(always_defined))
    cl._sometimes_initialized_attrs = find_sometimes_defined_attributes(
        m.blocks, self_reg, maybe_defined, dirty
    )

    mark_attr_initialization_ops(m.blocks, self_reg, maybe_defined, dirty)

    # Check if __init__ can run unpredictable code (leak 'self').
    any_dirty = False
    for b in m.blocks:
        for i, op in enumerate(b.ops):
            if dirty.after[b, i] and not isinstance(op, Return):
                any_dirty = True
                break
    cl.init_self_leak = any_dirty


def find_always_defined_attributes(
    blocks: list[BasicBlock],
    self_reg: Register,
    all_attrs: set[str],
    maybe_defined: AnalysisResult[str],
    maybe_undefined: AnalysisResult[str],
    dirty: AnalysisResult[None],
) -> set[str]:
    """Find attributes that are always initialized in some basic blocks.

    The analysis results are expected to be up-to-date for the blocks.

    Return a set of always defined attributes.
    """
    attrs = all_attrs.copy()
    for block in blocks:
        for i, op in enumerate(block.ops):
            # If an attribute we *read* may be undefined, it isn't always defined.
            if isinstance(op, GetAttr) and op.obj is self_reg:
                if op.attr in maybe_undefined.before[block, i]:
                    attrs.discard(op.attr)
            # If an attribute we *set* may be sometimes undefined and
            # sometimes defined, don't consider it always defined. Unlike
            # the get case, it's fine for the attribute to be undefined.
            # The set operation will then be treated as initialization.
            if isinstance(op, SetAttr) and op.obj is self_reg:
                if (
                    op.attr in maybe_undefined.before[block, i]
                    and op.attr in maybe_defined.before[block, i]
                ):
                    attrs.discard(op.attr)
            # Treat an op that might run arbitrary code as an "exit"
            # in terms of the analysis -- we can't do any inference
            # afterwards reliably.
            if dirty.after[block, i]:
                if not dirty.before[block, i]:
                    attrs = attrs & (
                        maybe_defined.after[block, i] - maybe_undefined.after[block, i]
                    )
                break
            if isinstance(op, ControlOp):
                for target in op.targets():
                    # Gotos/branches can also be "exits".
                    if not dirty.after[block, i] and dirty.before[target, 0]:
                        attrs = attrs & (
                            maybe_defined.after[target, 0] - maybe_undefined.after[target, 0]
                        )
    return attrs


def find_sometimes_defined_attributes(
    blocks: list[BasicBlock],
    self_reg: Register,
    maybe_defined: AnalysisResult[str],
    dirty: AnalysisResult[None],
) -> set[str]:
    """Find attributes that are sometimes initialized in some basic blocks."""
    attrs: set[str] = set()
    for block in blocks:
        for i, op in enumerate(block.ops):
            # Only look at possibly defined attributes at exits.
            if dirty.after[block, i]:
                if not dirty.before[block, i]:
                    attrs = attrs | maybe_defined.after[block, i]
                break
            if isinstance(op, ControlOp):
                for target in op.targets():
                    if not dirty.after[block, i] and dirty.before[target, 0]:
                        attrs = attrs | maybe_defined.after[target, 0]
    return attrs


def mark_attr_initialization_ops(
    blocks: list[BasicBlock],
    self_reg: Register,
    maybe_defined: AnalysisResult[str],
    dirty: AnalysisResult[None],
) -> None:
    """Tag all SetAttr ops in the basic blocks that initialize attributes.

    Initialization ops assume that the previous attribute value is the error value,
    so there's no need to decref or check for definedness.
    """
    for block in blocks:
        for i, op in enumerate(block.ops):
            if isinstance(op, SetAttr) and op.obj is self_reg:
                attr = op.attr
                if attr not in maybe_defined.before[block, i] and not dirty.after[block, i]:
                    op.mark_as_initializer()


GenAndKill = tuple[set[str], set[str]]


def attributes_initialized_by_init_call(op: Call) -> set[str]:
    """Calculate attributes that are always initialized by a super().__init__ call."""
    self_type = op.fn.sig.args[0].type
    assert isinstance(self_type, RInstance), self_type
    cl = self_type.class_ir
    return {a for base in cl.mro for a in base.attributes if base.is_always_defined(a)}


def attributes_maybe_initialized_by_init_call(op: Call) -> set[str]:
    """Calculate attributes that may be initialized by a super().__init__ call."""
    self_type = op.fn.sig.args[0].type
    assert isinstance(self_type, RInstance), self_type
    cl = self_type.class_ir
    return attributes_initialized_by_init_call(op) | cl._sometimes_initialized_attrs


class AttributeMaybeDefinedVisitor(BaseAnalysisVisitor[str]):
    """Find attributes that may have been defined via some code path.

    Consider initializations in class body and assignments to 'self.x'
    and calls to base class '__init__'.
    """

    def __init__(self, self_reg: Register) -> None:
        self.self_reg = self_reg

    def visit_branch(self, op: Branch) -> tuple[set[str], set[str]]:
        return set(), set()

    def visit_return(self, op: Return) -> tuple[set[str], set[str]]:
        return set(), set()

    def visit_unreachable(self, op: Unreachable) -> tuple[set[str], set[str]]:
        return set(), set()

    def visit_register_op(self, op: RegisterOp) -> tuple[set[str], set[str]]:
        if isinstance(op, SetAttr) and op.obj is self.self_reg:
            return {op.attr}, set()
        if isinstance(op, Call) and op.fn.class_name and op.fn.name == "__init__":
            return attributes_maybe_initialized_by_init_call(op), set()
        return set(), set()

    def visit_assign(self, op: Assign) -> tuple[set[str], set[str]]:
        return set(), set()

    def visit_assign_multi(self, op: AssignMulti) -> tuple[set[str], set[str]]:
        return set(), set()

    def visit_set_mem(self, op: SetMem) -> tuple[set[str], set[str]]:
        return set(), set()


def analyze_maybe_defined_attrs_in_init(
    blocks: list[BasicBlock], self_reg: Register, attrs_with_defaults: set[str], cfg: CFG
) -> AnalysisResult[str]:
    return run_analysis(
        blocks=blocks,
        cfg=cfg,
        gen_and_kill=AttributeMaybeDefinedVisitor(self_reg),
        initial=attrs_with_defaults,
        backward=False,
        kind=MAYBE_ANALYSIS,
    )


class AttributeMaybeUndefinedVisitor(BaseAnalysisVisitor[str]):
    """Find attributes that may be undefined via some code path.

    Consider initializations in class body, assignments to 'self.x'
    and calls to base class '__init__'.
    """

    def __init__(self, self_reg: Register) -> None:
        self.self_reg = self_reg

    def visit_branch(self, op: Branch) -> tuple[set[str], set[str]]:
        return set(), set()

    def visit_return(self, op: Return) -> tuple[set[str], set[str]]:
        return set(), set()

    def visit_unreachable(self, op: Unreachable) -> tuple[set[str], set[str]]:
        return set(), set()

    def visit_register_op(self, op: RegisterOp) -> tuple[set[str], set[str]]:
        if isinstance(op, SetAttr) and op.obj is self.self_reg:
            return set(), {op.attr}
        if isinstance(op, Call) and op.fn.class_name and op.fn.name == "__init__":
            return set(), attributes_initialized_by_init_call(op)
        return set(), set()

    def visit_assign(self, op: Assign) -> tuple[set[str], set[str]]:
        return set(), set()

    def visit_assign_multi(self, op: AssignMulti) -> tuple[set[str], set[str]]:
        return set(), set()

    def visit_set_mem(self, op: SetMem) -> tuple[set[str], set[str]]:
        return set(), set()


def analyze_maybe_undefined_attrs_in_init(
    blocks: list[BasicBlock], self_reg: Register, initial_undefined: set[str], cfg: CFG
) -> AnalysisResult[str]:
    return run_analysis(
        blocks=blocks,
        cfg=cfg,
        gen_and_kill=AttributeMaybeUndefinedVisitor(self_reg),
        initial=initial_undefined,
        backward=False,
        kind=MAYBE_ANALYSIS,
    )


def update_always_defined_attrs_using_subclasses(cl: ClassIR, seen: set[ClassIR]) -> None:
    """Remove attributes not defined in all subclasses from always defined attrs."""
    if cl in seen:
        return
    if cl.children is None:
        # Subclasses are unknown
        return
    removed = set()
    for attr in cl._always_initialized_attrs:
        for child in cl.children:
            update_always_defined_attrs_using_subclasses(child, seen)
            if attr not in child._always_initialized_attrs:
                removed.add(attr)
    cl._always_initialized_attrs -= removed
    seen.add(cl)


def detect_undefined_bitmap(cl: ClassIR, seen: set[ClassIR]) -> None:
    if cl.is_trait:
        return

    if cl in seen:
        return
    seen.add(cl)
    for base in cl.base_mro[1:]:
        detect_undefined_bitmap(cl, seen)

    if len(cl.base_mro) > 1:
        cl.bitmap_attrs.extend(cl.base_mro[1].bitmap_attrs)
    for n, t in cl.attributes.items():
        if t.error_overlap and not cl.is_always_defined(n):
            cl.bitmap_attrs.append(n)

    for base in cl.mro[1:]:
        if base.is_trait:
            for n, t in base.attributes.items():
                if t.error_overlap and not cl.is_always_defined(n) and n not in cl.bitmap_attrs:
                    cl.bitmap_attrs.append(n)
