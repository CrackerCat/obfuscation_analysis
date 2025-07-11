from typing import Iterable

import networkx as nx
from binaryninja.binaryview import BinaryView
from binaryninja.function import Function
from binaryninja.highlevelil import HighLevelILInstruction

from .mba.simplifer import get_simplifier
from .mba.slicing import backward_slice_basic_block_level
from .utils import (build_call_graph_from_function, find_corrupted_functions,
                    user_error)


def simplify_hlil_mba_slice_at(
    bv: BinaryView,
    instruction: HighLevelILInstruction,
) -> None:
    """
    Slice-and-simplify one HLIL instruction with msynth and drop the
    result as a user comment.

    Workflow
    --------
    1. Backward slice (single BB) –  
       `backward_slice_basic_block_level` resolves the instruction’s SSA
       dependency chain and translates the fully-inlined expression to
       Miasm IR.
    2. MBA simplification –  
       The cached simplifier canonicalises the Mixed-
       Boolean Arithmetic (MBA) expression.
    3. Annotate –  
       The simplified expression is attached as a decompiler comment at the
       instruction’s address.

    Error handling
    --------------
    * Any failure in translation or simplification is caught locally.  
    * A concise red line appears in Binary Ninja’s log; the full traceback
      is available when *Debug Log* is enabled.  
    * The function then returns early, leaving no partial comment behind.

    Parameters
    ----------
    bv :
        The active :class:`BinaryView`; needed only for architecture
        pointer-size information inside the slice routine.
    instruction :
        The HLIL instruction currently selected by the user.

    Side effects
    ------------
    * On success, a comment is written into
      ``instruction.function.source_function`` at `instruction.address`.
    * No value is returned; caller need not inspect a result.
    """

# backward slice in SSA form
    try:
        expr_m2 = backward_slice_basic_block_level(
            bv, instruction, instruction.function.ssa_form)
        # if assignment, only take right-hand side
        if expr_m2.is_assign():
            expr_m2 = expr_m2.src
    except Exception as err:
        user_error(
            f"Failed to translate HLIL expression at {hex(instruction.address)} to Miasm IR: {err}", exc=err)
        return

    # get simplifier
    simplifier = get_simplifier()
    if simplifier is None:
        return

    # simplify
    try:
        simplified = simplifier.simplify(expr_m2)
    except Exception as err:
        user_error(
            f"Could not simplify HLIL expression at address {hex(instruction.address)} using msynth: {err}", exc=err)
        return

    # add simplified expression as comment
    instruction.function.source_function.set_comment_at(
        instruction.address,
        str(simplified).replace("#0", ""),
    )


def identify_corrupted_functions(bv: BinaryView) -> None:
    """
    Emit a diagnostic list of functions with corrupted disassembly.

    A function is treated as corrupted, which typically happens if the linear sweep 
    created overlapping or undefined instructions—common in packed/obfuscated binaries.

    Parameters
    ----------
    bv : BinaryView
        Active BinaryView to scan.
    """
    for func in find_corrupted_functions(bv):
        print(f"Corrupted disassembly at {func.name} (0x{func.start:x})")


def remove_corrupted_functions(bv: BinaryView) -> None:
    """
    Remove (undefine) every corrupted function and force Binary Ninja to
    re-analyse the binary.

    Useful for cleaning up the function list when heavy obfuscation causes
    a flood of bogus or partially decoded functions.

    Note: In some cases this might be too aggressive.

    Parameters
    ----------
    bv : BinaryView
        Active BinaryView to clean up.
    """
    for func in find_corrupted_functions(bv):
        print(f"Removing corrupted function {func.name} (0x{func.start:x})")
        bv.remove_function(func)

    # Enforce re-analysis
    bv.update_analysis()


def inline_functions_recursively(bv: BinaryView, start_func: Function) -> None:
    """
    Recursively inline every function that is reachable from `start_func`
    so the decompiler can run a true cross‑function analysis on a single,
    fully inlined intermediate language.  The routine sets
    `Function.inline_during_analysis = True` for all descendants in an
    order that forces Binary Ninja to do *exactly one* global analysis
    pass.

    Efficiency highlights
    ---------------------
    - All descendants are discovered, including self‑ and mutually
      recursive functions.
    - Strongly‑connected components (SCCs) are collapsed, so every
      recursion group is handled as a single unit.
    - Analysis is paused while the flags are flipped, eliminating the
      overhead of spawning one job per function.

    Algorithm
    ---------
    1. Pause auto‑analysis
    2. Build the complete call graph rooted at `start_func`.
    3. Find strongly‑connected components (SCCs) so that every recursion
       group is handled as a single unit.
    4. Collapse SCCs to get the condensation DAG (always acyclic).
    5. Topologically sort the DAG (caller before callee) and iterate it in
       reverse order to obtain a bottom‑up sequence (callee before caller).
    6. For each SCC in that order, set `inline_during_analysis = True` on all
       member functions that are not flagged as `thunk`.
    7. Resume analysis and invoke `BinaryView.update_analysis_and_wait()` so
       that the core processes the queued work once.

    Complexity
    ----------
    - SCC computation           : O(V + E)
    - Condensation + topo sort  : O(V + E)
    - Flagging loop             : O(V)
      ------------------------------------
      Total                     : O(V + E)
    """
    # Pause automatic analysis so we can batch our changes.
    bv.set_analysis_hold(True)

    try:
        # Build the exhaustive call‑graph rooted at `start_func`.
        call_graph = build_call_graph_from_function(start_func)

        # Identify strongly‑connected components (recursion groups).
        sccs = list(nx.strongly_connected_components(call_graph))

        # Collapse each SCC into one node, producing an acyclic condensation DAG.
        condensed_dag = nx.condensation(call_graph, sccs)

        # Obtain a topological ordering (caller before callee).
        topo_order = list(nx.topological_sort(condensed_dag))

        # Process components in *reverse* topological order: bottom‑up.
        for component in reversed(topo_order):
            # Retrieve original Binary Ninja functions that belong to this SCC.
            for func in condensed_dag.nodes[component]['members']:
                # Flag the non-thunk function so Binary Ninja will inline its body at every
                # call‑site during the next analysis pass.
                if not func.is_thunk:
                    func.inline_during_analysis = True
    finally:
        # Re‑enable analysis regardless of what happened above.
        bv.set_analysis_hold(False)

    # Trigger exactly one analysis pass over all functions we just marked.
    bv.update_analysis_and_wait()
