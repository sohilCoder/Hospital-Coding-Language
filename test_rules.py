# test_rules.py - tests for rules.py
#
# Run with:  python test_rules.py
# (pytest also works if you have it installed)

import random

from rules import (CLASSES, DIAGNOSES, DRUG_CLASS, LABS, RuleError, check, compile_rules,
                   describe, did_you_mean, edit_distance, evaluate, load_patient, load_rules,
                   parse_program, run_compiled, run_rules, tokenize)


# ----- helpers -----

def parse_condition(text):
    """Parses just a condition by putting it inside a rule."""
    rules = parse_program(tokenize('rule "test" severity low when ' + text + ' alert "msg"'))
    return rules[0]["condition"]


def make_patient(labs, meds, diagnoses):
    return {"name": "test", "labs": labs, "meds": meds, "diagnoses": diagnoses}


def syntax_error(text):
    """Returns the error message for text, or None if there was no error."""
    try:
        parse_program(tokenize(text))
    except RuleError as error:
        return str(error)
    return None


# ----- edit distance -----

def test_edit_distance():
    assert edit_distance("kitten", "sitting") == 3
    assert edit_distance("", "abc") == 3
    assert edit_distance("same", "same") == 0
    assert edit_distance("potasium", "potassium") == 1


def test_did_you_mean():
    assert did_you_mean("potasium", LABS) == " (did you mean 'potassium'?)"
    assert did_you_mean("xyz", LABS) == ""            # nothing close: no suggestion


# ----- lexer -----

def test_tokens():
    tokens = tokenize('rule "x"\nwhen lab potassium >= 5.5  # comment')
    kinds = []
    for token in tokens:
        kinds.append(token[0])
    assert kinds == ["KEYWORD", "STRING", "KEYWORD", "KEYWORD", "NAME", "OP", "NUMBER", "END"]
    assert tokens[2] == ("KEYWORD", "when", 2)        # line numbers are counted


def test_lexer_errors():
    assert "missing closing quote" in syntax_error('rule "oops')
    assert "unexpected character" in syntax_error("lab potassium = 5")
    assert "bad number" in syntax_error("lab potassium > 5.5.5")


# ----- parser -----

def test_and_binds_tighter_than_or():
    tree = parse_condition("has diabetes or has ckd and has hypertension")
    assert tree["type"] == "or"
    assert tree["right"]["type"] == "and"              # diabetes or (ckd and hypertension)

    tree = parse_condition("has diabetes and has ckd or has hypertension")
    assert tree["type"] == "or"
    assert tree["left"]["type"] == "and"               # (diabetes and ckd) or hypertension


def test_parentheses_change_grouping():
    tree = parse_condition("(has diabetes or has ckd) and has hypertension")
    assert tree["type"] == "and"
    assert tree["left"]["type"] == "or"


def test_not_binds_tighter_than_and():
    tree = parse_condition("not has ckd and has diabetes")
    assert tree["type"] == "and"
    assert tree["left"]["type"] == "not"


def test_syntax_errors_name_the_line_and_problem():
    assert syntax_error('rule "x" severity low\nwhen lab potassium > \nalert "m"') == \
        "line 3: expected a number, found 'alert'"
    assert "did you mean 'taking'" in syntax_error('rule "x" severity low when takng drug metformin alert "m"')
    assert "did you mean 'high'" in syntax_error('rule "x" severity hgh when has ckd alert "m"')


# ----- checker -----

def test_checker_finds_unknown_names():
    rules = parse_program(tokenize('rule "x" severity low when lab potasium > 5 or has diabtes alert "m"'))
    assert check(rules) == ["line 1: unknown lab 'potasium' (did you mean 'potassium'?)",
                            "line 1: unknown diagnosis 'diabtes' (did you mean 'diabetes'?)"]


def test_checker_explains_drug_vs_class():
    rules = parse_program(tokenize('rule "x" severity low when taking class warfarin alert "m"'))
    assert "write 'taking drug warfarin'" in check(rules)[0]


def test_example_files():
    rules, errors = load_rules("examples/example.rules")
    assert errors == [] and len(rules) == 5
    rules, errors = load_rules("examples/mistakes.rules")
    assert len(errors) == 6


# ----- interpreter -----

def test_example_patients():
    rules, errors = load_rules("examples/example.rules")
    expected = {
        "patient1.json": ["High potassium on ACE inhibitor", "Bleeding risk: anticoagulant with NSAID"],
        "patient2.json": ["Metformin with reduced kidney function", "Low sodium"],
        "patient3.json": ["Diabetes without metformin"],
    }
    for filename in expected:
        patient = load_patient("examples/patients/" + filename)
        names = []
        for alert in run_rules(rules, patient):
            names.append(alert["rule"])
        assert names == expected[filename], filename


def test_most_severe_alerts_come_first():
    rules = parse_program(tokenize('rule "a" severity low when has ckd alert "m"\n'
                                   'rule "b" severity high when has ckd alert "m"'))
    alerts = run_rules(rules, make_patient({}, [], ["ckd"]))
    assert alerts[0]["rule"] == "b" and alerts[1]["rule"] == "a"


def test_evidence_only_includes_what_mattered():
    patient = make_patient({"creatinine": 2.0}, [], ["diabetes"])
    evidence = []
    # "has diabetes" is true, but its "and" partner is false, so it doesn't count.
    assert evaluate(parse_condition("(has diabetes and has ckd) or lab creatinine > 1.5"), patient, evidence)
    assert evidence == ["creatinine = 2 mg/dL (> 1.5)"]


def test_false_condition_adds_no_evidence():
    patient = make_patient({}, [], ["diabetes"])
    for text in ["has diabetes and has ckd", "has ckd or has hypertension", "not has diabetes"]:
        evidence = []
        assert evaluate(parse_condition(text), patient, evidence) == False
        assert evidence == [], text


def test_missing_lab_counts_as_false():
    patient = make_patient({}, [], [])
    assert evaluate(parse_condition("lab potassium > 5"), patient, []) == False
    assert evaluate(parse_condition("lab potassium <= 5"), patient, []) == False


# ----- compiler -----

def test_compiled_and():
    rules = parse_program(tokenize('rule "x" severity low when has ckd and has diabetes alert "m"'))
    assert compile_rules(rules) == [["HAS", "ckd"], ["JUMP_IF_FALSE", 3], ["HAS", "diabetes"], ["ALERT", 0]]


def test_compiled_or_skips_right_side():
    rules = parse_program(tokenize('rule "x" severity low when has ckd or lab potassium > 5 alert "m"'))
    code = compile_rules(rules)
    assert code[1] == ["JUMP_IF_TRUE", 3]               # jumps over the lab check
    assert run_compiled(code, make_patient({}, [], ["ckd"])) == [0]


# ----- random testing: the interpreter and the compiler must always agree -----

def random_condition(depth):
    if depth == 0 or random.random() < 0.3:
        choice = random.randint(1, 4)
        if choice == 1:
            op = random.choice([">", "<", ">=", "<=", "==", "!="])
            return {"type": "lab", "name": random.choice(list(LABS)), "op": op,
                    "value": float(random.randint(0, 9)), "line": 1}
        if choice == 2:
            return {"type": "drug", "name": random.choice(list(DRUG_CLASS)), "line": 1}
        if choice == 3:
            return {"type": "class", "name": random.choice(CLASSES), "line": 1}
        return {"type": "has", "name": random.choice(DIAGNOSES), "line": 1}

    choice = random.randint(1, 3)
    if choice == 1:
        return {"type": "not", "inside": random_condition(depth - 1)}
    kind = "and" if choice == 2 else "or"
    return {"type": kind, "left": random_condition(depth - 1), "right": random_condition(depth - 1)}


def random_patient():
    labs = {}
    for lab in LABS:
        if random.random() < 0.7:
            labs[lab] = float(random.randint(0, 9))
    meds = []
    for drug in DRUG_CLASS:
        if random.random() < 0.25:
            meds.append(drug)
    diagnoses = []
    for diagnosis in DIAGNOSES:
        if random.random() < 0.3:
            diagnoses.append(diagnosis)
    return make_patient(labs, meds, diagnoses)


def test_interpreter_and_compiler_agree():
    random.seed(1)
    for trial in range(300):
        # write 5 random rules as text, then parse them like a real rules file
        text = ""
        for i in range(5):
            text += 'rule "r' + str(i) + '" severity low when ' + describe(random_condition(4)) + ' alert "m"\n'
        rules = parse_program(tokenize(text))
        code = compile_rules(rules)

        for p in range(10):
            patient = random_patient()
            interpreted = []
            for i in range(len(rules)):
                if evaluate(rules[i]["condition"], patient, []):
                    interpreted.append(i)
            assert run_compiled(code, patient) == interpreted, text


# ----- run every test -----

if __name__ == "__main__":
    passed = 0
    failed = 0
    for name in sorted(dir()):
        if name.startswith("test_"):
            try:
                globals()[name]()
                print("PASS", name)
                passed += 1
            except Exception as error:
                print("FAIL", name, "-", repr(error))
                failed += 1
    print()
    print(passed, "passed,", failed, "failed")
