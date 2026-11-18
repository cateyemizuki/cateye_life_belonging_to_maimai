# 回归测试

使用 Python 3.10+、Pydantic 2、HTTPX、Pillow 和 Node.js 18+。

```sh
python -m pip install "pydantic>=2,<3" "httpx>=0.24" "Pillow>=9"
python -B tests/run_tests.py
```

入口逐个运行架构、日程与情绪、陪伴与媒体三个测试集，每个用例在独立进程中使用临时数据和 SDK 替身。WebUI 契约用例通过 Node.js 执行随包源码中的校验函数。

可通过环境变量 `CATEYE_TEST_DIR` 指向另一份待测源码。默认使用本仓库根目录。单独验证一个测试集时运行其 `test_astra_*.py` 文件，支持标准 `unittest` 用例名称参数。

修改对外入口、持久数据格式或跨模块回执时，在对应测试集中补充回归。新增辅助文件使用所属模块的名称前缀，保持进程与临时数据隔离。
