# This program is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation; either version 2 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc.,
# 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.
"""this module contains exceptions used in the astng library

:author:    Sylvain Thenault
:copyright: 2008-2009 LOGILAB S.A. (Paris, FRANCE)
:contact:   http://www.logilab.fr/ -- mailto:python-projects@logilab.org
:copyright: 2008-2009 Sylvain Thenault
:contact:   mailto:thenault@gmail.com
"""

"""this module contains utilities for rebuilding a compiler.ast
or _ast tree in order to get a single ASTNG representation
"""
from logilab.astng import ASTNGBuildingException, InferenceError
from logilab.astng import nodes
from logilab.astng.utils import ASTVisitor
from logilab.astng.raw_building import *
from logilab.astng.nodes_as_string import as_string


class RebuildVisitor(ASTVisitor):
    """Visitor to transform an AST to an ASTNG
    """
    def __init__(self):
        self.asscontext = None
        self._metaclass = None
        self._global_names = None
        self._delayed = []
        self.rebuilder = nodes.TreeRebuilder(self)

    def _add_local(self, node, name):
        if self._global_names and name in self._global_names[-1]:
            node.root().set_local(name, node)
        else:
            node.parent.set_local(name, node)

    def _push(self, node):
        """update the stack and init some parts of the Function or Class node
        """
        node.locals = {}
        node.parent.frame().set_local(node.name, node)

    def set_context(self, node, childnode):
        if isinstance(node, (nodes.Delete, nodes.Assign)):
            if childnode in node.targets:
                self.asscontext = node
            else:
                self.asscontext = None
        elif isinstance(node, (nodes.AugAssign, nodes.Comprehension, nodes.For)):
            if childnode is node.target:
                self.asscontext = node
            else:
                self.asscontext = None
        elif isinstance(node, nodes.Arguments):# and isinstance(node.parent, (nodes.Function, nodes.Lambda)):
            if childnode in node.args:
                self.asscontext = node
            else:
                self.asscontext = None
                
        elif isinstance(node, nodes.ExceptHandler):
            if childnode is node.name:
                self.asscontext = node
            else:
                self.asscontext = None
        elif isinstance(node, nodes.Subscript):
           self.asscontext = None # disable asscontext on subscripts to skip d[x] = y (item assigment)
    
    def walk(self, node):
        self._walk(node)
        delayed = self._delayed
        while delayed:
            dnode = delayed.pop(0)
            node_name = dnode.__class__.__name__.lower()
            self.delayed_visit_assattr(dnode)

    def _walk(self, node, parent=None):
        """default visit method, handle the parent attribute"""
        node.fromlineno = node.lineno
        node.parent = parent
        node.accept(self.rebuilder)
        handle_leave = node.accept(self)
        child = None
        for child in node.get_children():
            self.set_context(node, child)
            self._walk(child, node)
            if self.asscontext is child:
                self.asscontext = None
        node.set_line_info(child)
        if handle_leave:
            leave = getattr(self, "leave_" + node.__class__.__name__.lower() )
            leave(node)
        

    # general visit_<node> methods ############################################

    def visit_arguments(self, node):
        if node.vararg:
            node.parent.set_local(node.vararg, node)
        if node.kwarg:
            node.parent.set_local(node.kwarg, node)
        
    def visit_assign(self, node):
        return True
    
    def leave_assign(self, node):
        """leave an Assign node to become astng"""
        klass = node.parent.frame()
        if isinstance(klass, nodes.Class) and \
            isinstance(node.value, nodes.CallFunc) and \
            isinstance(node.value.func, nodes.Name):
            func_name = node.value.func.name
            if func_name in ('classmethod', 'staticmethod'):
                for ass_node in node.targets:
                    try:
                        meth = klass[ass_node.name]
                        if isinstance(meth, nodes.Function):
                            meth.type = func_name
                    except (AttributeError, KeyError):
                        continue
        elif getattr(node.targets[0], 'name', None) == '__metaclass__': # XXX check more...
            self._metaclass[-1] = 'type' # XXX get the actual metaclass

    def visit_class(self, node):
        """visit an Class node to become astng"""
        node.instance_attrs = {}
        self._push(node)
        for name, value in ( ('__name__', node.name),
                             ('__module__', node.root().name),
                             ('__doc__', node.doc) ):
            const = nodes.const_factory(value)
            const.parent = node
            node.locals[name] = [const]
        attach___dict__(node)
        self._metaclass.append(self._metaclass[-1])
        return True

    def leave_class(self, node):
        """leave a Class node -> pop the last item on the stack"""
        metaclass = self._metaclass.pop()
        if not node.bases:
            # no base classes, detect new / style old style according to
            # current scope
            node._newstyle = metaclass == 'type'
        node.basenames = [as_string(bnode) for bnode in node.bases]
    
    leave_classdef = leave_class

    def visit_decorators(self, node):
        """visiting an Decorators node: return True for leaving"""
        return True

    def leave_decorators(self, node):
        """python >= 2.4
        visit a Decorator node -> check for classmethod and staticmethod
        """
        for decorator_expr in node.nodes:
            if isinstance(decorator_expr, nodes.Name) and \
                   decorator_expr.name in ('classmethod', 'staticmethod'):
                node.parent.type = decorator_expr.name

    def visit_from(self, node):
        """visit an From node to become astng"""
        # add names imported by the import to locals
        for (name, asname) in node.names:
            if name == '*':
                try:
                    imported = node.root().import_module(node.modname)
                except ASTNGBuildingException:
                    continue
                for name in imported.wildcard_import_names():
                    node.parent.set_local(name, node)
            else:
                node.parent.set_local(asname or name, node)

    def visit_function(self, node):
        """visit an Function node to become astng"""
        self._global_names.append({})
        if isinstance(node.parent.frame(), nodes.Class):
            if node.name == '__new__':
                node.type = 'classmethod'
            else:
                node.type = 'method'
        self._push(node)
        return True

    def leave_function(self, node):
        """leave a Function node -> pop the last item on the stack"""
        self._global_names.pop()
    leave_functiondef = leave_function

    def visit_genexpr(self, node):
        """visit an ListComp node to become astng"""
        node.locals = {}

    def visit_assattr(self, node):
        """visit an Getattr node to become astng"""
        self._delayed.append(node) # FIXME
    visit_delattr = visit_assattr
    
    def visit_global(self, node):
        """visit an Global node to become astng"""
        if not self._global_names: # global at the module level, no effect
            return
        for name in node.names:
            self._global_names[-1].setdefault(name, []).append(node)

    def visit_import(self, node):
        """visit an Import node to become astng"""
        for (name, asname) in node.names:
            name = asname or name
            node.parent.set_local(name.split('.')[0], node)

    def visit_lambda(self, node):
        """visit an Keyword node to become astng"""
        node.locals = {}

    def visit_module(self, node):
        """visit an Module node to become astng"""
        self._metaclass = ['']
        self._global_names = []
        node.globals = node.locals = {}
        for name, value in ( ('__name__', node.name),
                             ('__file__', node.path),
                             ('__doc__', node.doc) ):
            const = nodes.const_factory(value)
            const.parent = node
            node.locals[name] = [const]
        attach___dict__(node)
        if node.package:
            const = nodes.const_factory(value)
            const.parent = node
            node.locals['__path__'] = [const]

    def visit_name(self, node):
        """visit an Name node to become astng"""
        try:
            cls, value = nodes.CONST_NAME_TRANSFORMS[node.name]
            node.__class__ = cls
            node.value = value
        except KeyError:
            pass

    def visit_assname(self, node):
        if self.asscontext is not None:
            self._add_local(node, node.name)
    visit_delname = visit_assname
    
    # # delayed methods

    def delayed_visit_assattr(self, node):
        """visit a AssAttr node -> add name to locals, handle members
        definition
        """
        try:
            frame = node.frame()
            for infered in node.expr.infer():
                if infered is nodes.YES:
                    continue
                try:
                    if infered.__class__ is nodes.Instance:
                        infered = infered._proxied
                        iattrs = infered.instance_attrs
                    else:
                        iattrs = infered.locals
                except AttributeError:
                    # XXX
                    import traceback
                    traceback.print_exc()
                    continue
                values = iattrs.setdefault(node.attrname, [])
                if node in values:
                    continue
                # get assign in __init__ first XXX useful ?
                if frame.name == '__init__' and values and not \
                       values[0].frame().name == '__init__':
                    values.insert(0, node)
                else:
                    values.append(node)
        except InferenceError:
            pass
