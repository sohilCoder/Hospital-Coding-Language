# Clinical Rule Language

A tiny programming language for clinical alert rules, written from scratch in plain Python with no libraries.

```
rule "High potassium on ACE inhibitor"
  severity high
  when lab potassium > 5.5 and taking class ACE_INHIBITOR
  alert "Potassium is high while on an ACE inhibitor. Consider holding it and rechecking."
```

The program reads rules like this, checks them for mistakes (with "did you mean" suggestions), and runs them against a patient to produce alerts with explanations. It can also compile the rules into instructions for a small virtual machine.

All patient data here is made up.

## How to run it

Needs Python 3. Run these from the project folder:

```
python rules.py check   examples/example.rules
python rules.py run     examples/example.rules examples/patients/patient1.json
python rules.py compile examples/example.rules examples/patients/patient2.json
python rules.py check   examples/mistakes.rules

python test_rules.py
```

Example output:

```
Patient 1001: 2 alert(s)

[HIGH] High potassium on ACE inhibitor
    Potassium is high while on an ACE inhibitor. Consider holding it and rechecking.
    because: potassium = 5.9 mmol/L (> 5.5); taking lisinopril (ACE_INHIBITOR)
```

```
error: line 6: unknown lab 'potasium' (did you mean 'potassium'?)
error: line 16: 'warfarin' is a drug, so write 'taking drug warfarin'
```

## The language

| Condition | Example |
|---|---|
| Lab result | `lab potassium > 5.5` (also `<`, `>=`, `<=`, `==`, `!=`) |
| Taking a drug | `taking drug warfarin` |
| Taking any drug in a class | `taking class NSAID` |
| Has a diagnosis | `has diabetes` |
| Combine | `and`, `or`, `not`, parentheses |

Grammar:

```
rule      -> "rule" STRING "severity" LEVEL "when" or_expr "alert" STRING
or_expr   -> and_expr ( "or" and_expr )*
and_expr  -> not_expr ( "and" not_expr )*
not_expr  -> "not" not_expr | primary
primary   -> "(" or_expr ")" | "lab" NAME OP NUMBER
           | "taking" "drug" NAME | "taking" "class" NAME | "has" NAME
```

## How it works

`rules.py` goes in five steps, in this order in the file:

1. **Lexer** (`tokenize`): reads the text one character at a time and splits it into tokens such as keywords, names, numbers, strings and comparisons, each with its line number.
2. **Parser** (`parse_or`, `parse_and`, `parse_not`, `parse_primary`): recursive descent, one function per grammar line. Each condition becomes a tree of dictionaries, like `{"type": "and", "left": ..., "right": ...}`. Because `parse_or` calls `parse_and`, `and` is grouped first, so it binds tighter than `or`.
3. **Checker** (`check`): looks up every name in the known labs, drugs and diagnoses. For unknown names it suggests the closest known one using **edit distance**: the fewest letter inserts, deletes or replacements between two words, computed with a dynamic programming table.
4. **Interpreter** (`evaluate`): walks the tree for one patient. `and` stops at the first false side and `or` stops at the first true side. It collects evidence, keeping only the facts that actually made the rule true.
5. **Compiler and VM** (`compile_rules`, `run_compiled`): turns each tree into a list of instructions. `and`/`or` become jump instructions that skip the right side when the left side already decides the answer. The jump target isn't known until the right side has been compiled, so the compiler fills it in afterward. The VM runs the instructions with a stack.

## Testing

`test_rules.py` has 19 tests, covering the lexer, operator precedence, error messages, the checker, expected alerts for each sample patient, evidence, and the compiled code.

The most important test generates 300 sets of random rules, runs them on random patients through **both** the interpreter and the compiled VM, and checks that they always agree. Two separate implementations checking each other catch bugs that hand-written tests miss.

## Limits

- A syntax error stops at the first mistake. Name errors are all reported together.
- A missing lab result counts as false.
- The lists of known labs, drugs and diagnoses are small, and are at the top of `rules.py`.
