import ast

with open('db.py', 'r', encoding='utf-8') as f:
    source = f.read()

tree = ast.parse(source)
for node in ast.walk(tree):
    if isinstance(node, ast.AsyncFunctionDef) or isinstance(node, ast.FunctionDef):
        print(f"Function: {node.name}")
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                if isinstance(child.func, ast.Attribute) and child.func.attr == 'table':
                    if isinstance(child.args[0], ast.Constant):
                        print(f"  Uses table: {child.args[0].value}")
