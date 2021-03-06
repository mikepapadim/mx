#
# ----------------------------------------------------------------------------------------------------
#
# Copyright (c) 2016, Oracle and/or its affiliates. All rights reserved.
# DO NOT ALTER OR REMOVE COPYRIGHT NOTICES OR THIS FILE HEADER.
#
# This code is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License version 2 only, as
# published by the Free Software Foundation.
#
# This code is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
# version 2 for more details (a copy is included in the LICENSE file that
# accompanied this code).
#
# You should have received a copy of the GNU General Public License version
# 2 along with this work; if not, write to the Free Software Foundation,
# Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Please contact Oracle, 500 Oracle Parkway, Redwood Shores, CA 94065 USA
# or visit www.oracle.com if you need additional information or have any
# questions.
#
# ----------------------------------------------------------------------------------------------------

import os
import zipfile
import pickle
import StringIO
import shutil
import itertools
from os.path import join, exists

import mx

class JavaModuleDescriptor(object):
    """
    Describes a Java module. This class closely mirrors ``java.lang.module.ModuleDescriptor``.

    :param str name: the name of the module
    :param dict exports: dict from a package defined by this module to the modules it's exported to. An
             empty list denotes an unqualified export.
    :param dict requires: dict from a module dependency to the modifiers of the dependency
    :param dict concealedRequires: dict from a module dependency to its concealed packages required by this module
    :param set uses: the service types used by this module
    :param dict provides: dict from a service name to the set of providers of the service defined by this module
    :param iterable packages: the packages defined by this module
    :param set conceals: the packages defined but not exported to everyone by this module
    :param str jarpath: path to module jar file
    :param JARDistribution dist: distribution from which this module was derived
    :param list modulepath: list of `JavaModuleDescriptor` objects for the module dependencies of this module
    :param bool boot: specifies if this module is in the boot layer
    """
    def __init__(self, name, exports, requires, uses, provides, packages=None, concealedRequires=None, jarpath=None, dist=None, modulepath=None, boot=False):
        self.name = name
        self.exports = exports
        self.requires = requires
        self.concealedRequires = concealedRequires if concealedRequires else {}
        self.uses = frozenset(uses)
        self.provides = provides
        exportedPackages = frozenset(exports.viewkeys())
        self.packages = exportedPackages if packages is None else frozenset(packages)
        assert len(exports) == 0 or exportedPackages.issubset(self.packages)
        self.conceals = self.packages - exportedPackages
        self.jarpath = jarpath
        self.dist = dist
        self.modulepath = modulepath
        self.boot = boot

    def __str__(self):
        return 'module:' + self.name

    def __repr__(self):
        return self.__str__()

    def __cmp__(self, other):
        assert isinstance(other, JavaModuleDescriptor)
        return cmp(self.name, other.name)

    @staticmethod
    def load(dist, jdk, fatalIfNotCreated=True):
        """
        Unpickles the module descriptor corresponding to a given distribution.

        :param str dist: the distribution for which to read the pickled object
        :param JDKConfig jdk: used to resolve pickled references to JDK modules
        :param bool fatalIfNotCreated: specifies whether to abort if a descriptor has not been created yet
        """
        _, moduleDir, _ = get_java_module_info(dist, fatalIfNotModule=True)  # pylint: disable=unpacking-non-sequence
        path = moduleDir + '.pickled'
        if not exists(path):
            if fatalIfNotCreated:
                mx.abort(path + ' does not exist')
            else:
                return None
        with open(path, 'rb') as fp:
            jmd = pickle.load(fp)
        jdkmodules = {m.name: m for m in jdk.get_modules()}
        resolved = []
        for name in jmd.modulepath:
            if name.startswith('dist:'):
                distName = name[len('dist:'):]
                resolved.append(as_java_module(mx.distribution(distName), jdk))
            else:
                resolved.append(jdkmodules[name])
        jmd.modulepath = resolved
        jmd.dist = mx.distribution(jmd.dist)
        return jmd

    def save(self):
        """
        Pickles this module descriptor to a file if it corresponds to a distribution.
        Otherwise, does nothing.

        :return: the path to which this module descriptor was pickled or None
        """
        dist = self.dist
        if not dist:
            # Don't pickle a JDK module
            return None
        _, moduleDir, _ = get_java_module_info(dist, fatalIfNotModule=True)  # pylint: disable=unpacking-non-sequence
        path = moduleDir + '.pickled'
        modulepath = self.modulepath
        self.modulepath = [m.name if not m.dist else 'dist:' + m.dist.name for m in modulepath]
        self.dist = dist.name
        try:
            with mx.SafeFileCreation(path) as sfc, open(sfc.tmpPath, 'wb') as f:
                pickle.dump(self, f)
        finally:
            self.modulepath = modulepath
            self.dist = dist

    def as_module_info(self):
        """
        Gets this module descriptor expressed as the contents of a ``module-info.java`` file.
        """
        out = StringIO.StringIO()
        print >> out, 'module ' + self.name + ' {'
        for dependency, modifiers in sorted(self.requires.iteritems()):
            modifiers_string = (' '.join(sorted(modifiers)) + ' ') if len(modifiers) != 0 else ''
            print >> out, '    requires ' + modifiers_string + dependency + ';'
        for source, targets in sorted(self.exports.iteritems()):
            targets_string = (' to ' + ', '.join(sorted(targets))) if len(targets) != 0 else ''
            print >> out, '    exports ' + source + targets_string + ';'
        for use in sorted(self.uses):
            print >> out, '    uses ' + use + ';'
        for service, providers in sorted(self.provides.iteritems()):
            print >> out, '    provides ' + service + ' with ' + ', '.join((p for p in providers)) + ';'
        for pkg in sorted(self.conceals):
            print >> out, '    // conceals: ' + pkg
        if self.jarpath:
            print >> out, '    // jarpath: ' + self.jarpath
        if self.dist:
            print >> out, '    // dist: ' + self.dist.name
        if self.modulepath:
            print >> out, '    // modulepath: ' + ', '.join([jmd.name for jmd in self.modulepath])
        if self.concealedRequires:
            for dependency, packages in sorted(self.concealedRequires.iteritems()):
                for package in sorted(packages):
                    print >> out, '    // concealed-requires: ' + dependency + '/' + package
        print >> out, '}'
        return out.getvalue()

def lookup_package(modulepath, package, importer):
    """
    Searches a given module path for the module defining a given package.

    :param list modulepath: a list of `JavaModuleDescriptors`
    :param str package: the name of the package to lookup
    :param str importer: the name of the module importing the package (use "<unnamed>" for the unnamed module)
    :return: if the package is found, then a tuple containing the defining module
             and a value of 'concealed' or 'exported' denoting the visibility of the package.
             Otherwise (None, None) is returned.
    """
    for jmd in modulepath:
        targets = jmd.exports.get(package, None)
        if targets is not None:
            if len(targets) == 0 or importer in targets:
                return jmd, 'exported'
            return jmd, 'concealed'
        elif package in jmd.conceals:
            return jmd, 'concealed'
    return (None, None)

def get_module_deps(dist):
    """
    Gets the JAR distributions and their constituent Java projects whose artifacts (i.e., class files and
    resources) are the input to the Java module jar created by `make_java_module` for a given distribution.

    :return: the set of `JARDistribution` objects and their constituent `JavaProject` transitive
             dependencies denoted by the ``moduledeps`` attribute
    """
    if dist.suite.getMxCompatibility().moduleDepsEqualDistDeps():
        return dist.archived_deps()

    if not hasattr(dist, '.module_deps'):
        roots = getattr(dist, 'moduledeps', [])
        if not roots:
            return roots
        for root in roots:
            if not root.isJARDistribution():
                mx.abort('moduledeps can (currently) only include JAR distributions: ' + str(root), context=dist)

        moduledeps = []
        def _visit(dep, edges):
            if dep is not dist:
                if dep.isJavaProject() or dep.isJARDistribution():
                    if dep not in moduledeps:
                        moduledeps.append(dep)
                else:
                    mx.abort('modules can (currently) only include JAR distributions and Java projects: ' + str(dep), context=dist)
        def _preVisit(dst, edge):
            return not dst.isJreLibrary() and not dst.isJdkLibrary()
        mx.walk_deps(roots, preVisit=_preVisit, visit=_visit)
        setattr(dist, '.module_deps', moduledeps)
    return getattr(dist, '.module_deps')

def as_java_module(dist, jdk, fatalIfNotCreated=True):
    """
    Gets the Java module created from a given distribution.

    :param JARDistribution dist: a distribution that defines a Java module
    :param JDKConfig jdk: a JDK with a version >= 9 that can be used to resolve references to JDK modules
    :param bool fatalIfNotCreated: specifies whether to abort if a descriptor has not been created yet
    :return: the descriptor for the module
    :rtype: `JavaModuleDescriptor`
    """
    if not hasattr(dist, '.javaModule'):
        jmd = JavaModuleDescriptor.load(dist, jdk, fatalIfNotCreated)
        if jmd:
            setattr(dist, '.javaModule', jmd)
        return jmd
    return getattr(dist, '.javaModule')

def get_java_module_info(dist, fatalIfNotModule=False):
    """
    Gets the metadata for the module derived from `dist`.

    :param JARDistribution dist: a distribution possibly defining a module
    :param bool fatalIfNotModule: specifies whether to abort if `dist` does not define a module
    :return: None if `dist` does not define a module otherwise a tuple containing
             the name of the module, the directory in which the class files
             (including module-info.class) for the module are staged and finally
             the path to the jar file containing the built module
    """
    if dist.suite.getMxCompatibility().moduleDepsEqualDistDeps():
        moduleName = getattr(dist, 'moduleName', None)
        if not moduleName:
            if fatalIfNotModule:
                mx.abort('Distribution ' + dist.name + ' does not define a module')
            return None
        assert len(moduleName) > 0, '"moduleName" attribute of distribution ' + dist.name + ' cannot be empty'
    else:
        if not get_module_deps(dist):
            if fatalIfNotModule:
                mx.abort('Module for distribution ' + dist.name + ' would be empty')
            return None
        moduleName = dist.name.replace('_', '.').lower()

    modulesDir = mx.ensure_dir_exists(join(dist.suite.get_output_root(), 'modules'))
    moduleDir = mx.ensure_dir_exists(join(modulesDir, moduleName))
    moduleJar = join(modulesDir, moduleName + '.jar')
    return moduleName, moduleDir, moduleJar

def _expand_package_info(dep, packages):
    """
    Converts a list of package names to a unique set of package names,
    expanding any '<package-info>' entry in the list to the set of
    packages in the project that contain a ``package-info.java`` file.
    """
    if '<package-info>' in packages:
        result = set((e for e in packages if e != '<package-info>'))
        result.update(mx._find_packages(dep, onlyPublic=True))
    else:
        result = set(packages)
    return result

def make_java_module(dist, jdk):
    """
    Creates a Java module from a distribution.

    :param JARDistribution dist: the distribution from which to create a module
    :param JDKConfig jdk: a JDK with a version >= 9 that can be used to compile the module-info class
    :return: the `JavaModuleDescriptor` for the created Java module
    """
    info = get_java_module_info(dist)
    if info is None:
        return None

    moduleName, moduleDir, moduleJar = info  # pylint: disable=unpacking-non-sequence
    mx.log('Building Java module ' + moduleName + ' from ' + dist.name)
    exports = {}
    requires = {}
    concealedRequires = {}
    uses = set()

    modulepath = list()
    usedModules = set()

    if dist.suite.getMxCompatibility().moduleDepsEqualDistDeps():
        moduledeps = dist.archived_deps()
        for dep in mx.classpath_entries(dist, includeSelf=False):
            if dep.isJARDistribution():
                jmd = as_java_module(dep, jdk, fatalIfNotCreated=False) or make_java_module(dep, jdk)
                modulepath.append(jmd)
                requires[jmd.name] = {jdk.get_transitive_requires_keyword()}
            elif (dep.isJdkLibrary() or dep.isJreLibrary()) and dep.is_provided_by(jdk):
                pass
            else:
                mx.abort(dist.name + ' cannot depend on ' + dep.name + ' as it does not define a module')
    else:
        moduledeps = get_module_deps(dist)

    # Append JDK modules to module path
    jdkModules = jdk.get_modules()
    if not isinstance(jdkModules, list):
        jdkModules = list(jdkModules)
    allmodules = modulepath + jdkModules

    javaprojects = [d for d in moduledeps if d.isJavaProject()]

    # Collect packages in the module first
    packages = set()
    for dep in javaprojects:
        packages.update(dep.defined_java_packages())

    for dep in javaprojects:
        uses.update(getattr(dep, 'uses', []))
        for pkg in getattr(dep, 'runtimeDeps', []):
            requires.setdefault(pkg, set(['static']))

        for pkg in itertools.chain(dep.imported_java_packages(projectDepsOnly=False), getattr(dep, 'imports', [])):
            # Only consider packages not defined by the module we're creating. This handles the
            # case where we're creating a module that will upgrade an existing upgradeable
            # module in the JDK such as jdk.internal.vm.compiler.
            if pkg not in packages:
                depModule, visibility = lookup_package(allmodules, pkg, moduleName)
                if depModule and depModule.name != moduleName:
                    requires.setdefault(depModule.name, set())
                    if visibility == 'exported':
                        # A distribution based module does not re-export its imported JDK packages
                        usedModules.add(depModule)
                    else:
                        assert visibility == 'concealed'
                        concealedRequires.setdefault(depModule.name, set()).add(pkg)
                        usedModules.add(depModule)

        # If an "exports" attribute is not present, all packages are exported
        for package in _expand_package_info(dep, getattr(dep, 'exports', dep.defined_java_packages())):
            exports.setdefault(package, [])

    provides = {}
    if exists(moduleDir):
        shutil.rmtree(moduleDir)
    for d in [dist] + [md for md in moduledeps if md.isJARDistribution()]:
        if d.isJARDistribution():
            with zipfile.ZipFile(d.path, 'r') as zf:
                # To compile module-info.java, all classes it references must either be given
                # as Java source files or already exist as class files in the output directory.
                # As such, the jar file for each constituent distribution must be unpacked
                # in the output directory.
                zf.extractall(path=moduleDir)
                names = frozenset(zf.namelist())
                for arcname in names:
                    if arcname.startswith('META-INF/services/') and not arcname == 'META-INF/services/':
                        service = arcname[len('META-INF/services/'):]
                        assert '/' not in service
                        provides.setdefault(service, set()).update(zf.read(arcname).splitlines())
                        # Service types defined in the module are assumed to be used by the module
                        serviceClass = service.replace('.', '/') + '.class'
                        if serviceClass in names:
                            uses.add(service)

    jmd = JavaModuleDescriptor(moduleName, exports, requires, uses, provides, packages=packages, concealedRequires=concealedRequires,
                               jarpath=moduleJar, dist=dist, modulepath=modulepath)

    # Compile module-info.class
    moduleInfo = join(moduleDir, 'module-info.java')
    with open(moduleInfo, 'w') as fp:
        print >> fp, jmd.as_module_info()
    javacCmd = [jdk.javac, '-d', moduleDir]
    jdkModuleNames = [m.name for m in jdkModules]
    modulepathJars = [m.jarpath for m in jmd.modulepath if m.jarpath and m.name not in jdkModuleNames]
    upgrademodulepathJars = [m.jarpath for m in jmd.modulepath if m.jarpath and m.name in jdkModuleNames]
    if modulepathJars:
        javacCmd.append('--module-path')
        javacCmd.append(os.pathsep.join(modulepathJars))
    if upgrademodulepathJars:
        javacCmd.append('--upgrade-module-path')
        javacCmd.append(os.pathsep.join(upgrademodulepathJars))
    javacCmd.append(moduleInfo)
    mx.run(javacCmd)

    # Create the module jar
    shutil.make_archive(moduleJar, 'zip', moduleDir)
    os.rename(moduleJar + '.zip', moduleJar)
    jmd.save()
    return jmd
