# rules.py - a tiny programming language for clinical alert rules
#
# A rule looks like this:
#
#   rule "High potassium on ACE inhibitor"
#     severity high
#     when lab potassium > 5.5 and taking class ACE_INHIBITOR
#     alert "Potassium is high while on an ACE inhibitor."
#
# The program works in steps:
#   1. tokenize()       text   -> list of tokens
#   2. parse_program()  tokens -> list of rules (each condition is a tree of dictionaries)
#   3. check()          finds unknown names and suggests fixes ("did you mean ...?")
#   4. evaluate()       runs a condition for one patient and explains the result
#   5. compile_rules()  turns the conditions into a list of simple instructions
#      run_compiled()   runs those instructions with a stack
#
# How to run it:
#   python rules.py check   RULES_FILE
#   python rules.py run     RULES_FILE PATIENT_FILE
#   python rules.py compile RULES_FILE PATIENT_FILE

import json
import sys


# ---------------------------------------------------------------------------
# Words the language knows about
# ---------------------------------------------------------------------------

LABS = {"potassium": "mmol/L", "sodium": "mmol/L", "creatinine": "mg/dL", "glucose": "mg/dL", "a1c": "%"}

DRUG_CLASS = {
    "lisinopril": "ACE_INHIBITOR",
    "enalapril": "ACE_INHIBITOR",
    "losartan": "ARB",
    "metformin": "BIGUANIDE",
    "insulin": "INSULIN",
    "ibuprofen": "NSAID",
    "naproxen": "NSAID",
    "warfarin": "ANTICOAGULANT",
    "apixaban": "ANTICOAGULANT",
}
CLASSES = list(set(DRUG_CLASS.values()))

DIAGNOSES = ["diabetes", "hypertension", "ckd", "heart_failure", "atrial_fibrillation"]

SEVERITIES = ["high", "medium", "low"]   # most severe first

KEYWORDS = ["rule", "severity", "when", "alert", "lab", "taking", "drug", "class",
            "has", "and", "or", "not"]


class RuleError(Exception):
    """Raised when a rules file has a mistake."""


# ---------------------------------------------------------------------------
# "Did you mean ...?" using edit distance
# ---------------------------------------------------------------------------

def edit_distance(a, b):
    """The fewest single-letter inserts, deletes or replacements that turn a into b.

    table[i][j] holds the distance between the first i letters of a
    and the first j letters of b. We fill the table one cell at a time.
    """
    table = []
    for i in range(len(a) + 1):
        table.append([0] * (len(b) + 1))

    for i in range(len(a) + 1):
        table[i][0] = i          # turn i letters into "" by deleting them all
    for j in range(len(b) + 1):
        table[0][j] = j          # turn "" into j letters by inserting them all

    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                replace_cost = 0
            else:
                replace_cost = 1
            delete = table[i - 1][j] + 1
            insert = table[i][j - 1] + 1
            replace = table[i - 1][j - 1] + replace_cost
            table[i][j] = min(delete, insert, replace)

    return table[len(a)][len(b)]


def did_you_mean(word, options):
    """Returns " (did you mean 'x'?)" if some option is close to word, otherwise ""."""
    best = None
    best_distance = None
    for option in options:
        distance = edit_distance(word.lower(), option.lower())
        if best_distance is None or distance < best_distance:
            best = option
            best_distance = distance

    allowed = max(1, len(word) // 3)     # about one typo for every three letters
    if best is not None and best_distance <= allowed:
        return " (did you mean '" + best + "'?)"
    return ""


# ---------------------------------------------------------------------------
# Step 1: Lexer - turn text into tokens
# ---------------------------------------------------------------------------
# Each token is a tuple: (kind, text, line_number)
# kinds: KEYWORD, NAME, NUMBER, STRING, OP, "(", ")", END

def tokenize(text):
    tokens = []
    i = 0
    line = 1

    while i < len(text):
        ch = text[i]

        if ch == "\n":
            line += 1
            i += 1

        elif ch == " " or ch == "\t" or ch == "\r":
            i += 1

        elif ch == "#":                              # comment: skip to the end of the line
            while i < len(text) and text[i] != "\n":
                i += 1

        elif ch == '"':                              # a string in quotes
            end = text.find('"', i + 1)
            if end == -1 or "\n" in text[i:end]:
                raise RuleError("line " + str(line) + ": missing closing quote")
            tokens.append(("STRING", text[i + 1:end], line))
            i = end + 1

        elif ch.isdigit():                           # a number like 5 or 5.5
            start = i
            while i < len(text) and (text[i].isdigit() or text[i] == "."):
                i += 1
            number = text[start:i]
            try:
                float(number)
            except ValueError:
                raise RuleError("line " + str(line) + ": bad number '" + number + "'")
            tokens.append(("NUMBER", number, line))

        elif ch.isalpha() or ch == "_":              # a word: keyword or name
            start = i
            while i < len(text) and (text[i].isalnum() or text[i] == "_"):
                i += 1
            word = text[start:i]
            if word in KEYWORDS:
                tokens.append(("KEYWORD", word, line))
            else:
                tokens.append(("NAME", word, line))

        elif text[i:i + 2] in [">=", "<=", "==", "!="]:   # two-character comparison
            tokens.append(("OP", text[i:i + 2], line))
            i += 2

        elif ch == ">" or ch == "<":
            tokens.append(("OP", ch, line))
            i += 1

        elif ch == "(" or ch == ")":
            tokens.append((ch, ch, line))
            i += 1

        else:
            raise RuleError("line " + str(line) + ": unexpected character '" + ch + "'")

    tokens.append(("END", "", line))
    return tokens


# ---------------------------------------------------------------------------
# Step 2: Parser - turn tokens into rules
# ---------------------------------------------------------------------------
# Grammar:
#   rule      -> "rule" STRING "severity" LEVEL "when" or_expr "alert" STRING
#   or_expr   -> and_expr ( "or" and_expr )*
#   and_expr  -> not_expr ( "and" not_expr )*
#   not_expr  -> "not" not_expr | primary
#   primary   -> "(" or_expr ")" | "lab" NAME OP NUMBER
#              | "taking" "drug" NAME | "taking" "class" NAME | "has" NAME
#
# Each grammar line is one function. Every function takes the token list and
# the current position, and returns (what it built, the next position).
#
# Conditions are stored as dictionaries, for example:
#   {"type": "and", "left": {...}, "right": {...}}
#   {"type": "lab", "name": "potassium", "op": ">", "value": 5.5, "line": 3}

def is_keyword(token, word):
    return token[0] == "KEYWORD" and token[1] == word


def expect(tokens, pos, kind, word, description):
    """Checks that the token at pos is what we expect. Returns (its text, next position)."""
    kind_found, text_found, line = tokens[pos]
    if kind_found != kind or (word is not None and text_found != word):
        if kind_found == "END":
            text_found = "end of file"
        raise RuleError("line " + str(line) + ": expected " + description + ", found '" + text_found + "'")
    return text_found, pos + 1


def parse_program(tokens):
    rules = []
    pos = 0
    while tokens[pos][0] != "END":
        rule, pos = parse_rule(tokens, pos)
        rules.append(rule)
    return rules


def parse_rule(tokens, pos):
    line = tokens[pos][2]
    _, pos = expect(tokens, pos, "KEYWORD", "rule", "'rule'")
    name, pos = expect(tokens, pos, "STRING", None, "the rule's name in quotes")
    _, pos = expect(tokens, pos, "KEYWORD", "severity", "'severity'")

    severity_line = tokens[pos][2]
    severity, pos = expect(tokens, pos, "NAME", None, "high, medium or low")
    if severity not in SEVERITIES:
        raise RuleError("line " + str(severity_line) + ": unknown severity '" + severity + "'"
                        + did_you_mean(severity, SEVERITIES))

    _, pos = expect(tokens, pos, "KEYWORD", "when", "'when'")
    condition, pos = parse_or(tokens, pos)
    _, pos = expect(tokens, pos, "KEYWORD", "alert", "'alert' (or 'and'/'or')")
    message, pos = expect(tokens, pos, "STRING", None, "the alert message in quotes")

    rule = {"name": name, "severity": severity, "condition": condition, "message": message, "line": line}
    return rule, pos


def parse_or(tokens, pos):
    left, pos = parse_and(tokens, pos)
    while is_keyword(tokens[pos], "or"):
        right, pos = parse_and(tokens, pos + 1)
        left = {"type": "or", "left": left, "right": right}
    return left, pos


def parse_and(tokens, pos):
    left, pos = parse_not(tokens, pos)
    while is_keyword(tokens[pos], "and"):
        right, pos = parse_not(tokens, pos + 1)
        left = {"type": "and", "left": left, "right": right}
    return left, pos


def parse_not(tokens, pos):
    if is_keyword(tokens[pos], "not"):
        inside, pos = parse_not(tokens, pos + 1)
        return {"type": "not", "inside": inside}, pos
    return parse_primary(tokens, pos)


def parse_primary(tokens, pos):
    token = tokens[pos]
    line = token[2]

    if token[0] == "(":
        inside, pos = parse_or(tokens, pos + 1)
        _, pos = expect(tokens, pos, ")", None, "')'")
        return inside, pos

    if is_keyword(token, "lab"):
        name, pos = expect(tokens, pos + 1, "NAME", None, "a lab name")
        op, pos = expect(tokens, pos, "OP", None, "a comparison like > or <")
        value, pos = expect(tokens, pos, "NUMBER", None, "a number")
        return {"type": "lab", "name": name, "op": op, "value": float(value), "line": line}, pos

    if is_keyword(token, "taking"):
        if is_keyword(tokens[pos + 1], "drug"):
            name, pos = expect(tokens, pos + 2, "NAME", None, "a drug name")
            return {"type": "drug", "name": name, "line": line}, pos
        if is_keyword(tokens[pos + 1], "class"):
            name, pos = expect(tokens, pos + 2, "NAME", None, "a drug class")
            return {"type": "class", "name": name, "line": line}, pos
        raise RuleError("line " + str(line) + ": expected 'drug' or 'class' after 'taking'")

    if is_keyword(token, "has"):
        name, pos = expect(tokens, pos + 1, "NAME", None, "a diagnosis")
        return {"type": "has", "name": name, "line": line}, pos

    hint = ""
    if token[0] == "NAME":
        hint = did_you_mean(token[1], ["lab", "taking", "has", "not"])
    raise RuleError("line " + str(line) + ": expected a condition, found '" + token[1] + "'" + hint)


# ---------------------------------------------------------------------------
# Step 3: Checker - find mistakes in names
# ---------------------------------------------------------------------------

def check(rules):
    """Returns a list of error messages (empty if everything is fine)."""
    errors = []
    names_seen = []
    for rule in rules:
        if rule["name"] in names_seen:
            errors.append("line " + str(rule["line"]) + ": the rule name \"" + rule["name"] + "\" is used twice")
        names_seen.append(rule["name"])
        check_condition(rule["condition"], errors)
    return errors


def check_condition(node, errors):
    kind = node["type"]

    if kind == "and" or kind == "or":
        check_condition(node["left"], errors)
        check_condition(node["right"], errors)

    elif kind == "not":
        check_condition(node["inside"], errors)

    else:
        name = node["name"]
        where = "line " + str(node["line"]) + ": "

        if kind == "lab" and name not in LABS:
            errors.append(where + "unknown lab '" + name + "'" + did_you_mean(name, LABS))

        elif kind == "drug" and name not in DRUG_CLASS:
            if name in CLASSES:
                errors.append(where + "'" + name + "' is a drug class, so write 'taking class " + name + "'")
            else:
                errors.append(where + "unknown drug '" + name + "'" + did_you_mean(name, DRUG_CLASS))

        elif kind == "class" and name not in CLASSES:
            if name in DRUG_CLASS:
                errors.append(where + "'" + name + "' is a drug, so write 'taking drug " + name + "'")
            else:
                errors.append(where + "unknown drug class '" + name + "'" + did_you_mean(name, CLASSES))

        elif kind == "has" and name not in DIAGNOSES:
            errors.append(where + "unknown diagnosis '" + name + "'" + did_you_mean(name, DIAGNOSES))


# ---------------------------------------------------------------------------
# Step 4: Interpreter - run a condition for one patient
# ---------------------------------------------------------------------------
# A patient is a dictionary loaded from a JSON file:
#   {"name": "...", "labs": {"potassium": 5.9}, "meds": ["lisinopril"], "diagnoses": ["hypertension"]}

def compare(a, op, b):
    if op == ">":
        return a > b
    if op == "<":
        return a < b
    if op == ">=":
        return a >= b
    if op == "<=":
        return a <= b
    if op == "==":
        return a == b
    return a != b                                  # op is "!="


def lab_is_true(patient, name, op, value):
    """False if the patient has no result for this lab."""
    if name not in patient["labs"]:
        return False
    return compare(patient["labs"][name], op, value)


def drugs_in_class(patient, drug_class):
    found = []
    for med in patient["meds"]:
        if DRUG_CLASS.get(med) == drug_class:
            found.append(med)
    return found


def show_number(x):
    """5.0 -> "5", 5.5 -> "5.5" """
    if x == int(x):
        return str(int(x))
    return str(x)


def describe(node):
    """Turns a condition back into text, e.g. for "not taking drug metformin"."""
    kind = node["type"]
    if kind == "and":
        return "(" + describe(node["left"]) + " and " + describe(node["right"]) + ")"
    if kind == "or":
        return "(" + describe(node["left"]) + " or " + describe(node["right"]) + ")"
    if kind == "not":
        return "not " + describe(node["inside"])
    if kind == "lab":
        return "lab " + node["name"] + " " + node["op"] + " " + show_number(node["value"])
    if kind == "drug":
        return "taking drug " + node["name"]
    if kind == "class":
        return "taking class " + node["name"]
    return "has " + node["name"]


def evaluate(node, patient, evidence):
    """Returns True or False. When True, adds the facts that made it true to evidence."""
    kind = node["type"]

    if kind == "lab":
        if not lab_is_true(patient, node["name"], node["op"], node["value"]):
            return False
        value = patient["labs"][node["name"]]
        evidence.append(node["name"] + " = " + show_number(value) + " " + LABS[node["name"]]
                        + " (" + node["op"] + " " + show_number(node["value"]) + ")")
        return True

    if kind == "drug":
        if node["name"] not in patient["meds"]:
            return False
        evidence.append("taking " + node["name"])
        return True

    if kind == "class":
        drugs = drugs_in_class(patient, node["name"])
        if len(drugs) == 0:
            return False
        evidence.append("taking " + ", ".join(drugs) + " (" + node["name"] + ")")
        return True

    if kind == "has":
        if node["name"] not in patient["diagnoses"]:
            return False
        evidence.append("has " + node["name"])
        return True

    if kind == "not":
        if evaluate(node["inside"], patient, []):
            return False
        evidence.append("not " + describe(node["inside"]))
        return True

    if kind == "and":
        # Each side collects its own evidence. We keep it only if BOTH sides are true.
        left_evidence = []
        if not evaluate(node["left"], patient, left_evidence):
            return False                          # left is false: don't even look at the right
        right_evidence = []
        if not evaluate(node["right"], patient, right_evidence):
            return False
        evidence.extend(left_evidence)
        evidence.extend(right_evidence)
        return True

    if kind == "or":
        # Stop at the first side that is true, and keep only its evidence.
        for side in [node["left"], node["right"]]:
            side_evidence = []
            if evaluate(side, patient, side_evidence):
                evidence.extend(side_evidence)
                return True
        return False

    raise RuleError("unknown condition type: " + kind)


def run_rules(rules, patient):
    """Returns the alerts for this patient, most severe first."""
    alerts = []
    for severity in SEVERITIES:                   # "high" first, then "medium", then "low"
        for rule in rules:
            if rule["severity"] != severity:
                continue
            evidence = []
            if evaluate(rule["condition"], patient, evidence):
                alerts.append({"rule": rule["name"], "severity": severity,
                               "message": rule["message"], "evidence": evidence})
    return alerts


# ---------------------------------------------------------------------------
# Step 5: Compiler and virtual machine
# ---------------------------------------------------------------------------
# The compiler turns each condition into a list of instructions.
# Each instruction is a list whose first item is its name:
#
#   ["LAB", name, op, value]    push True/False: does the lab compare true?
#   ["DRUG", name]              push True/False: is the patient taking this drug?
#   ["CLASS", name]             push True/False: any drug in this class?
#   ["HAS", name]               push True/False: does the patient have this diagnosis?
#   ["NOT"]                     pop a value, push the opposite
#   ["JUMP_IF_FALSE", target]   if the top value is False, jump to target and keep it there.
#                               Otherwise remove it and continue.            (used for "and")
#   ["JUMP_IF_TRUE", target]    the same, but jumps when the top value is True. (used for "or")
#   ["ALERT", rule_number]      pop a value; if it's True, that rule fires
#
# "and"/"or" become jumps so the right side is skipped when the left side
# already decides the answer.

def compile_rules(rules):
    code = []
    for number in range(len(rules)):
        compile_condition(rules[number]["condition"], code)
        code.append(["ALERT", number])
    return code


def compile_condition(node, code):
    kind = node["type"]

    if kind == "lab":
        code.append(["LAB", node["name"], node["op"], node["value"]])
    elif kind == "drug":
        code.append(["DRUG", node["name"]])
    elif kind == "class":
        code.append(["CLASS", node["name"]])
    elif kind == "has":
        code.append(["HAS", node["name"]])

    elif kind == "not":
        compile_condition(node["inside"], code)
        code.append(["NOT"])

    elif kind == "and" or kind == "or":
        compile_condition(node["left"], code)
        if kind == "and":
            jump = ["JUMP_IF_FALSE", None]
        else:
            jump = ["JUMP_IF_TRUE", None]
        code.append(jump)                      # we don't know where to jump yet...
        compile_condition(node["right"], code)
        jump[1] = len(code)                    # ...now we do: just past the right side


def run_compiled(code, patient):
    """Runs the instructions. Returns the numbers of the rules that fired."""
    stack = []
    fired = []
    pc = 0                                     # "program counter": which instruction is next

    while pc < len(code):
        instruction = code[pc]
        name = instruction[0]
        pc += 1

        if name == "LAB":
            stack.append(lab_is_true(patient, instruction[1], instruction[2], instruction[3]))
        elif name == "DRUG":
            stack.append(instruction[1] in patient["meds"])
        elif name == "CLASS":
            stack.append(len(drugs_in_class(patient, instruction[1])) > 0)
        elif name == "HAS":
            stack.append(instruction[1] in patient["diagnoses"])
        elif name == "NOT":
            value = stack.pop()
            stack.append(not value)
        elif name == "JUMP_IF_FALSE":
            if stack[-1] == False:
                pc = instruction[1]
            else:
                stack.pop()
        elif name == "JUMP_IF_TRUE":
            if stack[-1] == True:
                pc = instruction[1]
            else:
                stack.pop()
        elif name == "ALERT":
            value = stack.pop()
            if value:
                fired.append(instruction[1])

    return fired


def print_code(code, rules):
    rule_number = 0
    for i in range(len(code)):
        if i == 0 or code[i - 1][0] == "ALERT":
            print('; rule "' + rules[rule_number]["name"] + '"')
            rule_number += 1
        parts = []
        for item in code[i][1:]:
            if isinstance(item, float):
                parts.append(show_number(item))
            else:
                parts.append(str(item))
        print(str(i).rjust(4) + "  " + code[i][0].ljust(15) + " ".join(parts))


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def load_rules(filename):
    """Reads, parses and checks a rules file. Returns (rules, list of error messages)."""
    with open(filename) as f:
        text = f.read()
    try:
        rules = parse_program(tokenize(text))
    except RuleError as error:
        return [], [str(error)]
    return rules, check(rules)


def load_patient(filename):
    with open(filename) as f:
        patient = json.load(f)
    # fill in anything missing so the rest of the code can rely on it
    if "labs" not in patient:
        patient["labs"] = {}
    if "meds" not in patient:
        patient["meds"] = []
    if "diagnoses" not in patient:
        patient["diagnoses"] = []
    if "name" not in patient:
        patient["name"] = filename
    return patient


def main():
    usage = ("usage:\n"
             "  python rules.py check   RULES_FILE\n"
             "  python rules.py run     RULES_FILE PATIENT_FILE\n"
             "  python rules.py compile RULES_FILE PATIENT_FILE")
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "check":
        pass
    elif len(args) == 3 and (args[0] == "run" or args[0] == "compile"):
        pass
    else:
        print(usage)
        return

    rules, errors = load_rules(args[1])
    if len(errors) > 0:
        for error in errors:
            print("error:", error)
        return
    if args[0] == "check":
        print(args[1] + ": " + str(len(rules)) + " rules, no errors")
        return

    patient = load_patient(args[2])

    if args[0] == "run":
        alerts = run_rules(rules, patient)
        print(patient["name"] + ": " + str(len(alerts)) + " alert(s)\n")
        for alert in alerts:
            print("[" + alert["severity"].upper() + "] " + alert["rule"])
            print("    " + alert["message"])
            print("    because: " + "; ".join(alert["evidence"]) + "\n")

    else:
        code = compile_rules(rules)
        print_code(code, rules)
        fired = run_compiled(code, patient)
        print("\nRules that fired for " + patient["name"] + ":")
        if len(fired) == 0:
            print("  (none)")
        for number in fired:
            print("  " + rules[number]["name"])


if __name__ == "__main__":
    main()
