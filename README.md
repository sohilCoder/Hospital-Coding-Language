small programming language for hopistals which give alert rules. written in python.
lets clinicians write safety alerts in plain readable rules instead of code.

example of a rule (all rules have a name, serverity, condition, and alert message):\n
rule "High potassium on ACE inhibitor"
  severity high
  when lab potassium > 5.5 and taking class ACE_INHIBITOR
  alert "Potassium is high while on an ACE inhibitor. Consider holding it and rechecking."

example of describing a patient in json file:
{
  "name": "Patient 2001",
  "labs": {"potassium": 5.9},
  "meds": ["naproxen","lisinopril"],
  "diagnoses": ["heart_failure"]
}

all patient data here is made up.

test cases are in test_rules.py
run them using python test_rules.py

built from small fixed list of labs, drugs and diagnoses. this is a demo built on made-up data, not a clinically validated tool.
