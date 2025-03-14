import ast
import builtins
import sys
from _ast import NotEq, NotIn, IsNot, In
from typing import Optional, List

from monkey_patch.utils import get_key


class Or(list):
    pass


class AssertionVisitor(ast.NodeVisitor):
    def __init__(self, scope: Optional[dict] = None, patch_names: List[str] = [], wrapper_alias='test_func'):
        self.mocks = {}  # {args: output}
        self.scopes = [{}]  # Stack of scopes to mimic variable scope in code
        self.imported_modules = {}  # keys are module names, values are the actual modules
        self.patch_names = patch_names  # names of functions to patch
        if scope:
            self.local_scope = scope

        current_module = sys.modules[__name__]
        self.imported_modules[current_module.__name__] = current_module
        self.wrapper_alias = wrapper_alias

    def load_variable_values(self, var_name):
        for scope in reversed(self.scopes):
            if var_name in scope:
                return scope[var_name]
        return None

    def visit_Import(self, node):
        for name in node.names:
            self.imported_modules[name.name.split('.')[0]] = __import__(name.name)

    def visit_ImportFrom(self, node):
        module_name = node.module
        self.imported_modules[module_name.split('.')[0]] = __import__(module_name)

    def visit_Assign(self, node):
        target = node.targets[0]
        value = node.value
        if isinstance(target, ast.Name):
            if isinstance(value, ast.List):
                self.scopes[-1][target.id] = [self.get_value(elt) for elt in value.elts]
            elif isinstance(value, ast.Name):
                self.scopes[-1][target.id] = self.load_variable_values(value.id)
            else:
                self.scopes[-1][target.id] = self.get_value(value)

    def get_value(self, node):
        if isinstance(node, ast.Str):
            return node.s
        elif isinstance(node, ast.Num):
            return node.n
        elif isinstance(node, ast.NameConstant):
            return node.value
        elif isinstance(node, ast.List):
            return [self.get_value(e) for e in node.elts]
        elif isinstance(node, ast.Dict):
            keys = [self.get_value(k) for k in node.keys]
            values = [self.get_value(v) for v in node.values]
            return dict(zip(keys, values))
        elif isinstance(node, ast.Call):
            return self.extract_output(node)
        else:
            # This is a simplification; you might want to add more types
            raise NotImplementedError(f"Node type {type(node)} not supported")

    def process_assert(self, node, iter_name=None, evaluated_expr=None):

        if isinstance(node.test, ast.UnaryOp) and isinstance(node.test.op, ast.Not):
            # Special case for 'assert not ...'. We have to convert it into 'assert ... == None'
            operand = node.test.operand
            self.process_assert_helper(operand, ast.NameConstant(value=None), iter_name, op=ast.Eq())
            return

        left = node.test.left
        right = node.test.comparators[0]

        if isinstance(node.test.ops[0], (NotEq, NotIn, IsNot)):
            return

        if iter_name:
            for input_val in evaluated_expr:
                self.scopes.append({iter_name: input_val})
                self.process_assert_helper(left, right, iter_name, op=node.test.ops[0])
                self.scopes.pop()
        else:
            self.process_assert_helper(left, right, op=node.test.ops[0])

    def process_assert_with_tuple(self, node, iter_names, evaluated_expr):
        left = node.test.left
        right = node.test.comparators[0]

        if isinstance(node.test.ops[0], (NotEq, NotIn, IsNot)):
            return

        if evaluated_expr is not None:
            # Loop through the evaluated expression (which should be an iterable)
            for input_tuple in evaluated_expr:
                # Create a new scope for these variables
                self.scopes.append(dict(zip(iter_names, input_tuple)))

                if isinstance(node.test, ast.UnaryOp) and isinstance(node.test.op, ast.Not):
                    # Special case for 'assert not ...'
                    #operand = node.test.operand
                    self.process_assert_helper(left, ast.NameConstant(value=None), op=ast.Eq())
                else:
                    # Process the assert statement
                    self.process_assert_helper(left, right, op=node.test.ops[0])

                # Remove the temporary scope
                self.scopes.pop()

    def process_assert_helper(self, left, right, iter_name=None, op=None):
        if isinstance(left, ast.Call):
            if isinstance(left.func, ast.Name) and left.func.id in self.patch_names:
                self.process_assert_helper_lr(left, right, iter_name, op)
            elif isinstance(left.func, ast.Attribute) and left.func.attr in self.patch_names:
                self.process_assert_helper_lr(left, right, iter_name, op)

        elif isinstance(right, ast.Call):
            if isinstance(right.func, ast.Name) and right.func.id in self.patch_names:
                self.process_assert_helper_lr(right, left, iter_name, op)
            elif isinstance(right.func, ast.Attribute) and right.func.attr in self.patch_names:
                self.process_assert_helper_lr(right, left, iter_name, op)

    def process_assert_helper_lr(self, left, right, iter_name=None, op=None):
        input_args, input_kwargs = self.extract_args(left, iter_name)
        if isinstance(op, In):
            output = Or(self.extract_output(right))
        else:
            output = self.extract_output(right)

        key = get_key(input_args, input_kwargs)

        self.mocks[key] = output

    def extract_args(self, node, iter_name=None):
        # Assuming all arguments are positional
        args = []
        current_scope = self.scopes[-1]
        for arg in node.args:
            value = self.eval_expr(arg)
            if callable(value):
                if iter_name is not None:
                    arg_values = [current_scope.get(arg_name, None) for arg_name in iter_name]
                    value = value(*arg_values)
                else:
                    value = value()
            elif value is None:
                # If evaluation returns None, fall back to scope
                value = current_scope.get(arg.id, None)
            args.append(value)

        # Extract keyword arguments
        kwargs = {}
        for kw in node.keywords:
            kw_value = self.eval_expr(kw.value)
            if callable(kw_value):
                if iter_name is not None:
                    kw_values = [current_scope.get(arg_name, None) for arg_name in iter_name]
                    kw_value = kw_value(*kw_values)
                else:
                    kw_value = kw_value()
            elif kw_value is None:
                # If evaluation returns None, fall back to scope
                kw_value = current_scope.get(kw.arg, None)
            kwargs[kw.arg] = kw_value

        return args, kwargs

    def eval_expr(self, node):
        current_scope = self.scopes[-1]
        if isinstance(node, ast.Name):
            # Prioritize variables in the current scope over built-in functions
            if node.id in current_scope:
                return current_scope[node.id]
            elif hasattr(builtins, node.id):
                func = getattr(builtins, node.id)
                return func

        elif isinstance(node, ast.Call):
            # Function call, could be 'zip' or other
            func = self.eval_expr(node.func)  # Recursively evaluate the function expression
            if func is None:
                raise ValueError(
                    f"Function {node.func.id if isinstance(node.func, ast.Name) else ''} not found in the current scope or built-ins.")

            args = [self.eval_expr(arg) for arg in node.args]  # Recursively evaluate the arguments
            return func(*args)  # Assume func_name is a Python built-in, e.g., zip
        elif isinstance(node, ast.Num):
            return node.n  # Assume it's a number for simplicity
        elif isinstance(node, ast.Str):
            return node.s  # Assume it's a string for simplicity
        elif isinstance(node, ast.List):
            return [self.eval_expr(x) for x in node.elts]  # Assume it's a list for simplicity
        elif isinstance(node, ast.Tuple):
            return tuple([self.eval_expr(x) for x in node.elts])  # Assume it's a tuple for simplicity
        elif isinstance(node, ast.Dict):
            return {self.eval_expr(k): self.eval_expr(v) for k, v in zip(node.keys, node.values)}
        else:
            raise NotImplementedError(f"Node type {type(node).__name__} not handled yet")

    def instantiate(self, func, *args, **kwargs):
        """Instantiate a function with the given arguments and keyword arguments."""
        try:
            return func(*args, **kwargs)
        except TypeError as e:
            # If the function cannot be instantiated, return the function itself
            return func
        except Exception as e:
            raise e

    def extract_output(self, node, scope=None):
        # current_scope = self.scopes[-1]

        def eval_args(args, scope):
            return [self.extract_output(arg, scope) for arg in args]

        def eval_kwargs(keywords, scope):
            return {kw.arg: self.extract_output(kw.value, scope) for kw in keywords}

        if isinstance(node, ast.Num):
            return node.n
        elif isinstance(node, ast.Str):
            return node.s
        elif isinstance(node, ast.List):
            return [self.extract_output(elt, scope) for elt in node.elts]
        elif isinstance(node, ast.Dict):
            return {self.extract_output(k, scope): self.extract_output(v, scope) for k, v in
                    zip(node.keys, node.values)}
        elif isinstance(node, ast.Name):
            return self.load_variable_values(node.id)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                func_name = node.func.id
                args = eval_args(node.args, scope)
                kwargs = eval_kwargs(node.keywords, scope)

                # if func_name == "TodoItem":
                #     return TodoItem(*args, **kwargs)
                # elif func_name == "datetime":
                #     return datetime.datetime(*args, **kwargs)
                _globals = globals()
                _function_globals = self.local_scope[self.wrapper_alias].__globals__ if self.wrapper_alias in self.local_scope else {}
                # Generalized object instantiation
                if func_name in self.imported_modules:
                    module = self.imported_modules[func_name]
                    if hasattr(module, func_name):
                        func = getattr(module, func_name)
                        return self.instantiate(func, *args, **kwargs)

                elif func_name in _globals:
                    func = _globals[func_name]
                    return self.instantiate(func, *args, **kwargs)
                elif func_name in self.local_scope:
                    func = self.local_scope[func_name]
                    return self.instantiate(func, *args, **kwargs)
                elif func_name in _function_globals:
                    func = _function_globals[func_name]
                    return self.instantiate(func, *args, **kwargs)
                else:
                    raise NotImplementedError(f"Function {func_name} not handled yet")
            elif isinstance(node.func, ast.Attribute):
                func_name = node.func.attr
                # obj = self.extract_output(node.func.value, scope)
                _globals = globals()
                _function_globals = self.local_scope[self.wrapper_alias].__globals__ if self.wrapper_alias in self.local_scope else {}
                if isinstance(node.func.value, ast.Name):
                    module_or_class_name = node.func.value.id
                    # Check if module_or_class_name is an imported module
                    if module_or_class_name in self.imported_modules:
                        obj = self.imported_modules[module_or_class_name]
                    # Check if module_or_class_name is a globally defined class
                    elif module_or_class_name in _globals:
                        obj = _globals[module_or_class_name]
                    elif module_or_class_name in builtins.__dict__:
                        obj = builtins.__dict__[module_or_class_name]
                    elif module_or_class_name in self.local_scope:
                        obj = self.local_scope[module_or_class_name]
                    elif module_or_class_name in _function_globals:
                        obj = _function_globals[module_or_class_name]
                    else:
                        raise NotImplementedError(f"Module or class {module_or_class_name} not found")
                else:
                    obj = self.extract_output(node.func.value, scope)
                args = eval_args(node.args, scope)
                kwargs = eval_kwargs(node.keywords, scope)

                if hasattr(obj, func_name):
                    func = getattr(obj, func_name)
                    return self.instantiate(func, *args, **kwargs)
                else:
                    raise NotImplementedError(f"Attribute-based call {obj}.{func_name} not handled yet")

            # Add additional function handling here
        elif isinstance(node, ast.Attribute):
            # Handle attributes like datetime.datetime
            obj = self.extract_output(node.value, scope)
            return getattr(obj, node.attr)
        elif isinstance(node, ast.Tuple):
            return tuple(self.extract_output(elt, scope) for elt in node.elts)
        elif isinstance(node, ast.Constant):
            return node.value

        raise NotImplementedError(f"Node type {type(node).__name__} not handled yet")

    def visit_For(self, node):
        if isinstance(node.target, ast.Name):
            evaluated_expr = self.eval_expr(node.iter)
            iter_name = node.target.id
            if iter_name:
                for stmt in node.body:
                    if isinstance(stmt, ast.Assert):
                        self.process_assert(stmt, iter_name, evaluated_expr)
        # If loop variable is a tuple
        elif isinstance(node.target, ast.Tuple):
            iter_names = [elt.id for elt in node.target.elts]  # Extract names from tuple
            # iter_name = self.extract_variable_name(node.iter)
            evaluated_expr = self.eval_expr(node.iter)
            for stmt in node.body:
                if isinstance(stmt, ast.Assert):
                    self.process_assert_with_tuple(stmt, iter_names, evaluated_expr)

        self.scopes.pop()

    def extract_variable_name(self, node):
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                return node.func.id  # For now, just return the function name. You may also traverse args.
        return None  # Or raise an exception depending on your need

    def visit_Assert(self, node):
        self.process_assert(node)

    def visit_FunctionDef(self, node):
        self.scopes.append({})
        self.generic_visit(node)
        self.scopes.pop()
