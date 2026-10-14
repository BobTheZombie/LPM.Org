import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from lpm import app
from lpm.python_deps import satisfied


class PythonDependenciesTests(unittest.TestCase):
    def setUp(self):
        self.installed = {'python-packaging': {'version':'24.2', 'provides':[]}}

    def test_prefixed_and_unprefixed_reuse(self):
        self.assertTrue(satisfied('packaging>=24', self.installed))
        self.assertTrue(satisfied('python-packaging>=24', self.installed))

    def test_version_constraints_are_enforced(self):
        for requirement in ('packaging>=25', 'packaging<24', 'packaging!=24.2', 'packaging==24.1'):
            self.assertFalse(satisfied(requirement, self.installed))
        self.assertTrue(satisfied('packaging>=24,<25', self.installed))

    def test_normalization(self):
        self.assertTrue(satisfied('ordered_set>=2', {'python-ordered-set':{'version':'2.0','provides':[]}}))

    def test_native_package_name_does_not_shadow_python_distribution(self):
        self.assertFalse(satisfied('build', {'build':{'version':'1','provides':[]}}))

    def test_pypi_provides_and_markers(self):
        self.assertTrue(satisfied('packaging>=24', {'custom':{'version':'24.2','provides':['pypi(packaging)']}}))
        self.assertTrue(satisfied('missing; python_version < "2"', {}))

    def test_extras_and_urls_do_not_claim_unverified_satisfaction(self):
        self.assertFalse(satisfied('packaging[extra]', self.installed))
        self.assertFalse(satisfied('packaging @ https://example.org/package.whl', self.installed))

    def test_legacy_database_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = app._open_state_db(Path(tmp)/'db')
            try:
                app._store_installed_package(conn, app.PkgMeta('python-packaging','24.2'), [], True)
                conn.commit()
                installed = app.db_installed(conn)
                self.assertIn('packaging', installed['python-packaging']['provides'])
                self.assertTrue(satisfied('packaging>=24', installed))
            finally:
                conn.close()

    def test_repeated_recipe_build_skips_installed_pypi_builder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recipe = root/'demo.lpmbuild'
            recipe.write_text('NAME=lpmreusecheck\nVERSION=1\nARCH=noarch\nREQUIRES_PYTHON_DEPENDENCIES=("packaging>=24")\nbuild() { :; }\nstaging() { :; }\n')
            dbpath = root/'db'
            conn = app._open_state_db(dbpath)
            app._store_installed_package(conn, app.PkgMeta('python-packaging','24.2'), [], True)
            conn.commit(); conn.close()
            with patch.object(app, 'db', side_effect=lambda: app._open_state_db(dbpath)), \
                 patch.object(app, 'build_python_package_from_pip') as pip_build, \
                 patch.object(app, 'sandboxed_run'), \
                 patch.object(app, 'build_package', side_effect=lambda staged, meta, out, **kwargs: out.write_text('test')), \
                 patch.object(app, 'generate_install_script', return_value='true'):
                for _ in range(2):
                    app.run_lpmbuild(recipe, root, prompt_install=False)
                pip_build.assert_not_called()


if __name__ == '__main__':
    unittest.main()
