import sys, time
sys.path.insert(0, '.')
from mcp_servers.git_server import create_isolated_branch, apply_patch_and_test

print('starting create_isolated_branch...', flush=True)
t0 = time.time()
result = create_isolated_branch(
    repo='C:/Users/Anuranan Chetia/Downloads/warden/demo-repo',
    base_branch='main',
    new_branch='warden/fix-manual-test',
)
print(f'create_isolated_branch returned {result} after {time.time()-t0:.1f}s', flush=True)

diff = '''--- app.py
+++ app.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b  # bug
+    return a + b  # bug
'''
print('starting apply_patch_and_test...', flush=True)
t0 = time.time()
result = apply_patch_and_test(
    repo='C:/Users/Anuranan Chetia/Downloads/warden/demo-repo',
    diff=diff,
    test_cmd='pytest -q',
)
print(f'apply_patch_and_test returned {result} after {time.time()-t0:.1f}s', flush=True)