from typing import List, Optional, Union, Tuple
import itertools
from typing_extensions import Literal
from dataclasses import dataclass
import textwrap
from tools.codegen import local
from tools.codegen.context import method_with_native_function, native_function_manager
from tools.codegen.utils import Target, mapMaybe
from tools.codegen.model import (BaseType, OptionalType, DispatchKey, NativeFunction,
                                 NativeFunctionsGroup, SchemaKind, FunctionSchema,
                                 TensorOptionsArguments, ListType,
                                 DeviceCheckType, Argument, assert_never,
                                 is_cuda_dispatch_key, BackendIndex,
                                 gets_generated_out_inplace_wrapper)
from tools.codegen.api.types import (BaseTy, BaseCppType, BaseCType, OptionalCType,
                                     Binding, ConstRefCType, NamedCType,
                                     CppSignature, CppSignatureGroup,
                                     Expr, MutRefCType, kernel_signature,
                                     DispatcherSignature, VectorCType, intT)
import tools.codegen.api.meta as meta
import tools.codegen.api.cpp as cpp
import tools.codegen.api.structured as structured
from tools.codegen.api.translate import translate
from tools.codegen.selective_build.selector import SelectiveBuilder

# Generates {backend}_lazy_ir.h and .cpp
#
valueListT = BaseCppType('at', 'ValueList') # TODO, not sure this type exists
valueT = BaseCppType('ir', 'Value')
intArrayT = BaseCppType('std', 'vector<int64_t>') # TODO this should probably be different

def process_ir_types(func: FunctionSchema) -> Tuple[List[NamedCType], List[NamedCType], List[NamedCType]]:
    """
    We handle Tensor arguments specially as 'IR Values', and everything else (?) as usual.

    TODO, a less awful way of achieving this than what I have done, which is basically to reimplement
    half of argumenttype_type and fall back to it for non tensors.
    """
    types = []
    value_types = []
    scalar_types = []
    for arg in func.schema_order_arguments():
        t = arg.type
        mutable = arg.is_write
        binds = arg.name
        if isinstance(t, BaseType):
            if t.name == BaseTy.Tensor:
                if mutable and not local.use_const_ref_for_mutable_tensors():
                    types.append(NamedCType(binds, MutRefCType(BaseCType(valueT))))
                    value_types.append(NamedCType(binds, MutRefCType(BaseCType(valueT))))
                else:
                    types.append(NamedCType(binds, ConstRefCType(BaseCType(valueT))))
                    value_types.append(NamedCType(binds, MutRefCType(BaseCType(valueT))))
            else:
                types.append(cpp.argumenttype_type(t, mutable=mutable, binds=binds))
                scalar_types.append(cpp.argumenttype_type(t, mutable=mutable, binds=binds))
        elif isinstance(t, OptionalType):
            if str(t.elem) == 'Tensor':
                if mutable and not local.use_const_ref_for_mutable_tensors():
                    types.append(NamedCType(binds, MutRefCType(BaseCType(valueT))))  # TODO: fix this discrepancy
                    value_types.append(NamedCType(binds, MutRefCType(BaseCType(valueT))))  # TODO: fix this discrepancy
                else:
                    types.append(NamedCType(binds, ConstRefCType(OptionalCType(BaseCType(valueT)))))
                    value_types.append(NamedCType(binds, ConstRefCType(OptionalCType(BaseCType(valueT)))))
            else:
                types.append(cpp.argumenttype_type(t, mutable=mutable, binds=binds))
                scalar_types.append(cpp.argumenttype_type(t, mutable=mutable, binds=binds))
        elif isinstance(t, ListType):
            if str(t.elem) == 'Tensor':
                types.append(NamedCType(binds, BaseCType(valueListT)))
                value_types.append(NamedCType(binds, BaseCType(valueListT)))
            elif str(t.elem) == 'Tensor?':
                types.append(NamedCType(binds, ConstRefCType(ListCType(OptionalCType(BaseCType(valueT))))))
                value_types.append(NamedCType(binds, ConstRefCType(ListCType(OptionalCType(BaseCType(valueT))))))
            elif str(t.elem) == 'int':
                types.append(NamedCType(binds, BaseCType(intArrayT)))
                scalar_types.append(NamedCType(binds, BaseCType(intArrayT)))
            else:
                types.append(cpp.argumenttype_type(t, mutable=mutable, binds=binds))
                scalar_types.append(cpp.argumenttype_type(t, mutable=mutable, binds=binds))
        else:
            raise AssertionError(f"unrecognized type {repr(t)}")
    return types, value_types, scalar_types

def ir_node_name(func: FunctionSchema):
    return str(func.name).lower().capitalize()

@dataclass(frozen=True)
class LazyIR:
    backend_index: BackendIndex

    @method_with_native_function
    def __call__(self, f: Union[NativeFunctionsGroup, NativeFunction]) -> List[str]:
        func = f.functional.func if isinstance(f, NativeFunctionsGroup) else f.func
        if func.name in self.backend_index.index:
            return self.gen(f)
        else:
            return []

    def gen(self, f: Union[NativeFunctionsGroup, NativeFunction]) -> List[str]:
        # for now, we just want one IR class decl and soon after also the method defs
        # and we use the functional version not out/inplace.
        func = f.functional.func if isinstance(f, NativeFunctionsGroup) else f.func
        class_name = ir_node_name(func)

        all_types, value_types, scalar_types = process_ir_types(func)
        node_ctor_args = ", ".join([f"{i.cpp_type()} {i.name}" for i in all_types])
        scalar_initializers = ",\n        ".join([f"{t.name}_({t.name})" for t in scalar_types])
        scalar_decls = "\n  ".join([f"{t.cpp_type()} {t.name}_;" for t in scalar_types])
        scalar_hashes = ", ".join([f.name for f in scalar_types])
        base_ctor_value_args = []
        for t in value_types:
            if isinstance(t.type.elem, BaseCType):
                base_ctor_value_args.append(f"{t.name}")
            elif isinstance(t.type.elem, OptionalCType):
                base_ctor_value_args.append(f"{t.name}.has_value() ? {t.name}.value() : kNullValue")
            else:
                assert False, ""
        base_ctor_value_args = ", ".join(base_ctor_value_args)
        members_to_string = "\n    ".join([f'lazy_tensors::ToString("{t.name}", {t.name}_, ss);' for t in scalar_types])
        return [f"""\
class {class_name} : public Node {{
 public:
  {class_name}({node_ctor_args})
      : Node(ir::OpKind(at::aten::{func.name.name}),
              {{{base_ctor_value_args}}},
              /*num_outputs=*/{len(func.returns)},
              lazy_tensors::util::MHash({scalar_hashes})),
        {scalar_initializers}
  {{
      throw std::runtime_error("need to hash scalars properly");
  }}

  std::string ToString() const override {{
    std::stringstream ss;
    ss << Node::ToString();
    {members_to_string}
    return ss.str();
  }}

  {scalar_decls}

}};

""", ]
