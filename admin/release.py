# -*- test-case-name: admin.test.test_release -*-
# Copyright Hybrid Logic Ltd.  See LICENSE file for details.

"""
Helper utilities for the Flocker release process.

XXX This script is not automatically checked by buildbot. See
https://clusterhq.atlassian.net/browse/FLOC-397
"""

import os
import sys
import tempfile

from collections import namedtuple
from effect import (
    Effect, sync_perform, ComposedDispatcher, base_dispatcher)
from effect.do import do
from characteristic import attributes
from git import Repo

from twisted.python.filepath import FilePath
from twisted.python.usage import Options, UsageError
from twisted.python.constants import Names, NamedConstant

import flocker

from flocker.docs import (
    get_doc_version,
    get_pre_release,
    is_pre_release,
    is_release,
    is_weekly_release,
    target_release,
)
from flocker.provision._install import ARCHIVE_BUCKET

from .aws import (
    boto_dispatcher,
    UpdateS3RoutingRule,
    ListS3Keys,
    DeleteS3Keys,
    CopyS3Keys,
    DownloadS3KeyRecursively,
    UploadToS3Recursively,
    CreateCloudFrontInvalidation,

)

from .yum import (
    yum_dispatcher,
    CreateRepo,
    DownloadPackagesFromRepository,
)


__all__ = ['rpm_version', 'make_rpm_version']

# Use characteristic or pyrsistent instead.
# https://clusterhq.atlassian.net/browse/FLOC-1223
rpm_version = namedtuple('rpm_version', 'version release')


def make_rpm_version(flocker_version):
    """
    Parse the Flocker version generated by versioneer into an RPM compatible
    version and a release version.
    See: http://fedoraproject.org/wiki/Packaging:NamingGuidelines#Pre-Release_packages  # noqa

    :param flocker_version: The versioneer style Flocker version string.
    :return: An ``rpm_version`` tuple containing a ``version`` and a
        ``release`` attribute.
    """
    # E.g. 0.1.2-69-gd2ff20c-dirty
    # tag+distance+shortid+dirty
    parts = flocker_version.split('-')
    tag, remainder = parts[0], parts[1:]
    for suffix in ('pre', 'dev'):
        parts = tag.rsplit(suffix, 1)
        if len(parts) == 2:
            # A pre or dev suffix was present. ``version`` is the part before
            # the pre and ``suffix_number`` is the part after the pre, but
            # before the first dash.
            version = parts.pop(0)
            suffix_number = parts[0]
            if suffix_number.isdigit():
                # Given pre or dev number X create a 0 prefixed, `.` separated
                # string of version labels. E.g.
                # 0.1.2pre2  becomes
                # 0.1.2-0.pre.2
                release = ['0', suffix, suffix_number]
            else:
                # Non-integer pre or dev number found.
                raise Exception(
                    'Non-integer value "{}" for "{}". '
                    'Supplied version {}'.format(
                        suffix_number, suffix, flocker_version))
            break
    else:
        # Neither of the expected suffixes was found, the tag can be used as
        # the RPM version
        version = tag
        release = ['1']

    if remainder:
        # The version may also contain a distance, shortid which
        # means that there have been changes since the last
        # tag. Additionally there may be a ``dirty`` suffix which
        # indicates that there are uncommitted changes in the
        # working directory.  We probably don't want to release
        # untagged RPM versions, and this branch should probably
        # trigger and error or a warning. But for now we'll add
        # that extra information to the end of release number.
        # See https://clusterhq.atlassian.net/browse/FLOC-833
        release.extend(remainder)

    return rpm_version(version, '.'.join(release))


class NotTagged(Exception):
    """
    Raised if publishing to production and the version being published version
    isn't tagged.
    """


class NotARelease(Exception):
    """
    Raised if trying to publish documentation to, or packages for a version
    that isn't a release.
    """


class DocumentationRelease(Exception):
    """
    Raised if trying to upload packages for a documentation release.
    """


class Environments(Names):
    """
    The environments that documentation can be published to.
    """
    PRODUCTION = NamedConstant()
    STAGING = NamedConstant()


class TagExists(Exception):
    """
    Raised if trying to release a version for which a tag already exists.
    """


class BranchExists(Exception):
    """
    Raised if trying to release a version for which a branch already exists.
    """


class BaseBranchDoesNotExist(Exception):
    """
    Raised if trying to release a version for which the expected base branch
    does not exist.
    """


class MissingPreRelease(Exception):
    """
    Raised if trying to release a pre-release for which the previous expected
    pre-release does not exist.
    """


class NoPreRelease(Exception):
    """
    Raised if trying to release a marketing release if no pre-release exists.
    """


@attributes([
    'documentation_bucket',
    'cloudfront_cname',
    'dev_bucket',
])
class DocumentationConfiguration(object):
    """
    The configuration for publishing documentation.

    :ivar bytes documentation_bucket: The bucket to publish documentation to.
    :ivar bytes cloudfront_cname: a CNAME associated to the cloudfront
        distribution pointing at the documentation bucket.
    :ivar bytes dev_bucket: The bucket buildbot uploads documentation to.
    """

DOCUMENTATION_CONFIGURATIONS = {
    Environments.PRODUCTION:
        DocumentationConfiguration(
            documentation_bucket="clusterhq-docs",
            cloudfront_cname="docs.clusterhq.com",
            dev_bucket="clusterhq-dev-docs"),
    Environments.STAGING:
        DocumentationConfiguration(
            documentation_bucket="clusterhq-staging-docs",
            cloudfront_cname="docs.staging.clusterhq.com",
            dev_bucket="clusterhq-dev-docs"),
}


@do
def publish_docs(flocker_version, doc_version, environment):
    """
    Publish the flocker documentation.

    :param bytes flocker_version: The version of flocker to publish the
        documentation for.
    :param bytes doc_version: The version to publish the documentation as.
        Attempting to publish documentation as a documentation version
        publishes it as the version being updated.
    :param Environments environment: The environment to publish the
        documentation to.
    :raises NotARelease: Raised if trying to publish to a version that isn't a
        release.
    :raises NotTagged: Raised if publishing to production and the version being
        published version isn't tagged.
    """
    if not (is_release(doc_version)
            or is_weekly_release(doc_version)
            or is_pre_release(doc_version)):
        raise NotARelease()

    if environment == Environments.PRODUCTION:
        if get_doc_version(flocker_version) != doc_version:
            raise NotTagged()
    configuration = DOCUMENTATION_CONFIGURATIONS[environment]

    dev_prefix = '%s/' % (flocker_version,)
    version_prefix = 'en/%s/' % (get_doc_version(doc_version),)

    is_dev = not is_release(doc_version)
    if is_dev:
        stable_prefix = "en/devel/"
    else:
        stable_prefix = "en/latest/"

    # Get the list of keys in the new documentation.
    new_version_keys = yield Effect(
        ListS3Keys(bucket=configuration.dev_bucket,
                   prefix=dev_prefix))
    # Get the list of keys already existing for the given version.
    # This should only be non-empty for documentation releases.
    existing_version_keys = yield Effect(
        ListS3Keys(bucket=configuration.documentation_bucket,
                   prefix=version_prefix))

    # Copy the new documentation to the documentation bucket.
    yield Effect(
        CopyS3Keys(source_bucket=configuration.dev_bucket,
                   source_prefix=dev_prefix,
                   destination_bucket=configuration.documentation_bucket,
                   destination_prefix=version_prefix,
                   keys=new_version_keys))

    # Delete any keys that aren't in the new documentation.
    yield Effect(
        DeleteS3Keys(bucket=configuration.documentation_bucket,
                     prefix=version_prefix,
                     keys=existing_version_keys - new_version_keys))

    # Update the redirect for the stable URL (en/latest/ or en/devel/)
    # to point to the new version. Returns the old target.
    old_prefix = yield Effect(
        UpdateS3RoutingRule(bucket=configuration.documentation_bucket,
                            prefix=stable_prefix,
                            target_prefix=version_prefix))

    # If we have changed versions, get all the keys from the old version
    if old_prefix:
        previous_version_keys = yield Effect(
            ListS3Keys(bucket=configuration.documentation_bucket,
                       prefix=old_prefix))
    else:
        previous_version_keys = set()

    # The changed keys are the new keys, the keys that were deleted from this
    # version, and the keys for the previous version.
    changed_keys = (new_version_keys |
                    existing_version_keys |
                    previous_version_keys)

    # S3 serves /index.html when given /, so any changed /index.html means
    # that / changed as well.
    # Note that we check for '/index.html' but remove 'index.html'
    changed_keys |= {key_name[:-len('index.html')]
                     for key_name in changed_keys
                     if key_name.endswith('/index.html')}

    # Always update the root.
    changed_keys |= {''}

    # The full paths are all the changed keys under the stable prefix, and
    # the new version prefix. This set is slightly bigger than necessary.
    changed_paths = {prefix + key_name
                     for key_name in changed_keys
                     for prefix in [stable_prefix, version_prefix]}

    # Invalidate all the changed paths in cloudfront.
    yield Effect(
        CreateCloudFrontInvalidation(cname=configuration.cloudfront_cname,
                                     paths=changed_paths))


class PublishDocsOptions(Options):
    """
    Arguments for ``publish-docs`` script.
    """

    optParameters = [
        ["flocker-version", None, flocker.__version__,
         "The version of flocker from which the documentation was built."],
        ["doc-version", None, None,
         "The version to publish the documentation as.\n"
         "This will differ from \"flocker-version\" for staging uploads.\n"
         "Attempting to publish documentation as a documentation version "
         "publishes it as the version being updated.\n"],
    ]

    optFlags = [
        ["production", None, "Publish documentation to production."],
    ]

    environment = Environments.STAGING

    def parseArgs(self):
        if self['doc-version'] is None:
            self['doc-version'] = get_doc_version(self['flocker-version'])

        if self['production']:
            self.environment = Environments.PRODUCTION


def publish_docs_main(args, base_path, top_level):
    """
    :param list args: The arguments passed to the script.
    :param FilePath base_path: The executable being run.
    :param FilePath top_level: The top-level of the flocker repository.
    """
    options = PublishDocsOptions()

    try:
        options.parseOptions(args)
    except UsageError as e:
        sys.stderr.write("%s: %s\n" % (base_path.basename(), e))
        raise SystemExit(1)

    try:
        sync_perform(
            dispatcher=ComposedDispatcher([boto_dispatcher, base_dispatcher]),
            effect=publish_docs(
                flocker_version=options['flocker-version'],
                doc_version=options['doc-version'],
                environment=options.environment,
                ))
    except NotARelease:
        sys.stderr.write("%s: Can't publish non-release.\n"
                         % (base_path.basename(),))
        raise SystemExit(1)
    except NotTagged:
        sys.stderr.write(
            "%s: Can't publish non-tagged version to production.\n"
            % (base_path.basename(),))
        raise SystemExit(1)


class UploadOptions(Options):
    """
    Options for uploading packages.
    """
    optParameters = [
        ["flocker-version", None, flocker.__version__,
         "The version of Flocker to upload packages for."],
        ["target", None, ARCHIVE_BUCKET,
         "The bucket to upload packages to."],
        ["build-server", None,
         b'http://build.clusterhq.com',
         "The URL of the build-server."],
    ]


FLOCKER_PACKAGES = [
    b'clusterhq-python-flocker',
    b'clusterhq-flocker-cli',
    b'clusterhq-flocker-node',
]


@do
def update_repo(rpm_directory, target_bucket, target_key, source_repo,
                packages, flocker_version, distro_name, distro_version):
    """
    Update ``target_bucket`` yum repository with ``packages`` from
    ``source_repo`` repository.

    :param FilePath rpm_directory: Temporary directory to download
        repository to.
    :param bytes target_bucket: S3 bucket to upload repository to.
    :param bytes target_key: Path within S3 bucket to upload repository to.
    :param bytes source_repo: Repository on the build server to get packages
        from.
    :param list packages: List of bytes, each specifying the name of a package
        to upload to the repository.
    :param bytes flocker_version: The version of flocker to upload packages
        for.
    :param distro_name: The name of the distribution to upload packages for.
    :param distro_version: The distro_version of the distribution to upload
        packages for.
    """
    rpm_directory.createDirectory()

    yield Effect(DownloadS3KeyRecursively(
        source_bucket=target_bucket,
        source_prefix=target_key,
        target_path=rpm_directory,
        filter_extensions=('.rpm',)))

    downloaded_packages = yield Effect(DownloadPackagesFromRepository(
        source_repo=source_repo,
        target_path=rpm_directory,
        packages=packages,
        flocker_version=flocker_version,
        distro_name=distro_name,
        distro_version=distro_version,
        ))

    new_metadata = yield Effect(CreateRepo(repository_path=rpm_directory))

    yield Effect(UploadToS3Recursively(
        source_path=rpm_directory,
        target_bucket=target_bucket,
        target_key=target_key,
        files=downloaded_packages | new_metadata,
        ))


@do
def upload_rpms(scratch_directory, target_bucket, version, build_server):
    """
    Upload RPMS from build server to yum repository.

    :param FilePath scratch_directory: Temporary directory to download
        repository to.
    :param bytes target_bucket: S3 bucket to upload repository to.
    :param bytes version: Version to download RPMs for.
    :param bytes build_server: Server to download new RPMs from.
    """
    if not (is_release(version)
            or is_weekly_release(version)
            or is_pre_release(version)):
        raise NotARelease()

    if get_doc_version(version) != version:
        raise DocumentationRelease()

    is_dev = not is_release(version)
    if is_dev:
        target_distro_suffix = "-testing"
    else:
        target_distro_suffix = ""


    operating_systems = [
        {'distro': 'fedora', 'version': '20', 'arch': 'x86_64'},
        {'distro': 'centos', 'version': '7', 'arch': 'x86_64'},
    ]

    for operating_system in operating_systems:
        yield update_repo(
            rpm_directory=scratch_directory.child(
                b'{}-{}-{}'.format(
                    operating_system['distro'],
                    operating_system['version'],
                    operating_system['arch'])),
            target_bucket=target_bucket,
            target_key=os.path.join(
                operating_system['distro'] + target_distro_suffix,
                operating_system['version'],
                operating_system['arch']),
            source_repo=os.path.join(
                build_server, b'results/omnibus',
                version,
                b'{}-{}'.format(
                    operating_system['distro'],
                    operating_system['version'])),
            packages=FLOCKER_PACKAGES,
            flocker_version=version,
            distro_name=operating_system['distro'],
            distro_version=operating_system['version'],
        )


def publish_rpms_main(args, base_path, top_level):
    """
    The ClusterHQ yum repository contains packages for Flocker, as well as the
    dependencies which aren't available in Fedora 20 or CentOS 7. It is
    currently hosted on Amazon S3. When doing a release, we want to add the
    new Flocker packages, while preserving the existing packages in the
    repository. To do this, we download the current repository, add the new
    package, update the metadata, and then upload the repository.

    :param list args: The arguments passed to the script.
    :param FilePath base_path: The executable being run.
    :param FilePath top_level: The top-level of the flocker repository.
    """
    options = UploadOptions()

    try:
        options.parseOptions(args)
    except UsageError as e:
        sys.stderr.write("%s: %s\n" % (base_path.basename(), e))
        raise SystemExit(1)

    dispatcher = ComposedDispatcher([boto_dispatcher, yum_dispatcher,
                                     base_dispatcher])

    try:
        scratch_directory = FilePath(tempfile.mkdtemp(
            prefix=b'flocker-upload-rpm-'))

        sync_perform(
            dispatcher=dispatcher,
            effect=upload_rpms(
                scratch_directory=scratch_directory,
                target_bucket=options['target'],
                version=options['flocker-version'],
                build_server=options['build-server'],
                ))

    except NotARelease:
        sys.stderr.write("%s: Can't upload RPMs for a non-release."
                         % (base_path.basename(),))
        raise SystemExit(1)
    except DocumentationRelease:
        sys.stderr.write("%s: Can't upload RPMs for a documentation release."
                         % (base_path.basename(),))
        raise SystemExit(1)
    finally:
        scratch_directory.remove()


def create_release_branch(version, repo_dir=None):
    """
    checkout a new Git branch to make changes on and later tag as a release.
    This branch is created from a different branch which depends on the release
    type and sometimes which pre-releases have preceeded this.
    """
    # pip install gitpython==1.0.0
    # TODO flake8
    # TODO wrapper for this with options, handles each exception
    # TODO param docs - none is default, here
    # TODO separate get_base_branch and create branch

    if not (is_release(version)
            or is_weekly_release(version)
            or is_pre_release(version)):
        raise NotARelease()

    repo = Repo(repo_dir)

    existing_tags = [tag for tag in repo.tags if tag.name == version]
    if existing_tags:
        raise TagExists()

    release_branch_prefix = 'release/flocker-'
    existing_branches = [
        branch for branch in repo.branches if
        branch.name == release_branch_prefix + version]
    if existing_branches:
        raise BranchExists()

    if is_weekly_release(version):
        base_branch_name = 'master'
    elif is_pre_release(version) and get_pre_release(version) == 1:
        base_branch_name = 'master'
    elif get_doc_version(version) != version:
        base_branch_name = release_branch_prefix + get_doc_version(version)
    else:
        if is_pre_release(version):
            target_version = target_release(version)
        else:
            target_version = version

        pre_releases = [
            tag.name for tag in repo.tags if
            is_pre_release(tag.name) and
            target_version == target_release(tag.name)]

        if not pre_releases:
            raise NoPreRelease()

        latest_pre_release = sorted(
            pre_releases,
            key=lambda pre_release: get_pre_release(pre_release))[-1]

        if (is_pre_release(version) and
            get_pre_release(version) > get_pre_release(latest_pre_release) + 1):
            raise MissingPreRelease()

        base_branch_name = release_branch_prefix + latest_pre_release

    # We create a new branch from a branch, not a tag, because a maintenance
    # or documentation change may have been applied to the branch and not the
    # tag.
    try:
        base_branch = [
            branch for branch in repo.branches if
            branch.name == base_branch_name][0]
    except IndexError:
        raise BaseBranchDoesNotExist()

    base_branch.checkout(b=release_branch_prefix + version)
    return base_branch
