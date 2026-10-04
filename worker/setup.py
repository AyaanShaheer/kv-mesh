from setuptools import setup, Extension
from setuptools.command.build_ext import build_ext
import sys
import setuptools

class get_pybind_include(object):
    """Helper class to determine the pybind11 include path"""
    def __str__(self):
        import pybind11
        return pybind11.get_include()

ext_modules = [
    Extension(
        'kv_cpp',
        ['cpp_extension/memory_manager.cpp'],
        include_dirs=[
            get_pybind_include(),
        ],
        language='c++'
    ),
]

setup(
    name='kv_cpp',
    version='0.1.0',
    description='C++ Memory Extension for KV-Mesh',
    ext_modules=ext_modules,
)