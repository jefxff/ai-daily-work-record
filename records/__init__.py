"""records 应用包。

显式声明为常规包：缺 __init__.py 时本目录是 PEP 420 命名空间包，
Django 能正常加载，但 unittest 的测试发现（manage.py test 默认从项目根
扫描 test*.py）会跳过该目录，导致本模块测试从来不参与全量回归。
"""
