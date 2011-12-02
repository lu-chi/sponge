import time
import logging
from celery.registry import tasks
from celery import registry
from celery.task import Task
from sponge.utils import get_pulp_server
from sponge.utils import repo as repo_utils
from sponge.models import CeleryTaskTracker
from pulp.client.api.repository import RepositoryAPI
from pulp.client.api.task import TaskAPI, task_end, task_succeeded
from pulp.client.api.server import ServerRequestError

logger = logging.getLogger(__name__)

class TaskExecutionError(Exception):
    pass


class TrackedTask(Task):
    def update(self, msg, state="PROGRESS"):
        logger.info("%s, state=%s" % (msg, state))
        return self.update_state(state=state, meta=msg)

    def __call__(self, *args, **kwargs):
        if "user" in kwargs:
            user = kwargs.pop("user")
            CeleryTaskTracker.objects.create(taskid=self.request.id,
                                             taskclass=self.__class__.__name__,
                                             owner=user)
            get_pulp_server(user=user)
        return Task.__call__(self, *args, **kwargs)


class CreateRepo(TrackedTask):
    def run(self, repo_id, groups=None, name=None, arch=None, url=None,
            gpgkeys=None, cksum="sha1", filters=None):
        if groups is None:
            groups = []
        if gpgkeys is None:
            gpgkeys = []
        if filters is None:
            filters = []

        success = True
        repoapi = RepositoryAPI()
        errors = []
        keylist = repo_utils.get_keylist(gpgkeys, errors=errors)
        for error in errors:
            self.update(error)
            success = False

        try:
            repoapi.create(repo_id, name, arch,
                           feed=url,
                           relative_path=repo_id,
                           groupid=groups,
                           gpgkeys=keylist,
                           checksum_type=cksum)
        except ServerRequestError, err:
            raise TaskExecutionError("Could not create repo %s: %s" %
                                     (repo_id, err[1]))

        self.update("Repository %s created, running sync" % repo_id)

        try:
            repo_utils.sync_foreground(repo_id)
            self.update("Repository %s synced, adding filters" % repo_id)
        except Exception, err:
            success = False
            self.update("Repository %s failed to sync: %s" % (repo_id, err),
                        state="ERROR")

        repo = repo_utils.get_repo(repo_id)
        errors = []
        if repo_utils.set_filters(clone, parent['filters'], errors=errors):
            self.update("Filters added to %s" % clone_id)
        else:
            success = False
            self.update("Error adding filters to %s: %s" % (clone_id,
                                                            ", ".join(errors)),
                        state="ERROR")

        errors = []
        if not repo_utils.rebalance_sync_schedule(errors):
            for error in errors:
                success = False
                self.update(error, state="ERROR")

        if success:
            return "Successfully created repo %s" % repo['name']
        else:
            raise TaskExecutionError("Created %s (%s), but encountered errors"
                                     % (name, repo_id))

tasks.register(CreateRepo)


class CloneRepo(TrackedTask):
    def run(self, clone_id, name=None, parent=None, groups=None, filters=None):
        if groups is None:
            groups = []
        if filters is None:
            filters = []

        repoapi = RepositoryAPI()
        success = True

        # we have to remove the schedule from a repo before we can clone
        # it
        try:
            schedule = repo_utils.remove_schedule(parent)
            self.update("Removed schedule from %s" % parent['id'])
        except ServerRequestError, err:
            success = False
            raise TaskExecutionError("Could not remove schedule from %s: %s" %
                                     (parent['id'], err[1]))

        try:
            repoapi.clone(parent['id'], clone_id, name,
                          relative_path=clone_id)
            self.update("Cloned %s to %s" % (parent['id'], clone_id))
        except ServerRequestError, err:
            success = False
            repo_utils.restore_schedule(parent, schedule)
            raise TaskExecutionError("Could not clone %s as %s: %s" %
                                     (parent['id'], clone_id, err[1]))

        clone = repo_utils.get_repo(clone_id)

        errors = []
        if repo_utils.set_gpgkeys(clone, parent['keys'].values(),
                                  errors=errors):
            self.update("Set GPG keys for %s" % clone_id)
        else:
            success = False
            self.update("Error setting GPG keys for %s: %s" %
                        (clone_id, ", ".join(errors)),
                        state="ERROR")

        errors = []
        if repo_utils.set_groups(clone, groups, errors=errors):
            self.update("Set groups for %s" % clone_id)
        else:
            success = False
            self.update("Error setting groups for %s: %s" % (clone_id,
                                                             ", ".join(errors)),
                        state="ERROR")

        try:
            repo_utils.restore_schedule(parent, schedule)
            self.update("Restored sync schedule %s to %s" % (schedule,
                                                             parent['id']))
        except ServerRequestError, err:
            success = False
            self.update("Could not restore sync schedule %s to %s: %s" %
                        (schedule, parent['id'], err[1]),
                        state="ERROR")

        try:
            repo_utils.sync_foreground(clone_id)
            self.update("Repository %s synced, adding filters" % clone_id)
        except Exception, err:
            success = False
            self.update("Repository %s failed to sync: %s" % (clone_id, err),
                        state="ERROR")

        errors = []
        if repo_utils.set_filters(clone, parent['filters'], errors=errors):
            self.update("Filters added to %s" % clone_id)
        else:
            success = False
            self.update("Error adding filters to %s: %s" % (clone_id,
                                                            ", ".join(errors)),
                        state="ERROR")

        errors = []
        if not repo_utils.rebalance_sync_schedule(errors):
            for error in errors:
                success = False
                self.update(error, state="ERROR")

        if success:
            return "Successfully cloned %s (%s) to %s (%s)" % (parent['name'],
                                                               parent['id'],
                                                               name, clone_id)
        else:
            raise TaskExecutionError("Cloned %s (%s) to %s (%s), but "
                                     "encountered errors" %
                                     (parent['name'], parent['id'],
                                      name, clone_id))

tasks.register(CloneRepo)


class SyncRepo(TrackedTask):
    def run(self, repo_id):
        logger.error("tasks: %s" % registry.tasks.regular().keys())
        try:
            repo_utils.sync_foreground(repo_id)
        except Exception, err:
            raise TaskExecutionError("Repository %s failed to sync: %s" %
                                     (repo_id, err))
        return "Repository %s synced" % repo_id

tasks.register(SyncRepo)


class RebuildMetadata(TrackedTask):
    def run(self, repo_id):
        taskapi = TaskAPI()
        repoapi = RepositoryAPI()
        running = repoapi.running_task(repoapi.sync_list(repo_id))
        if running is not None:
            raise TaskExecutionError("Metadata rebuild for repository %s "
                                     "already in progress" % repo_id)
        task = repoapi.sync(repo_id)
        while not task_end(task):
            time.sleep(1)
            task = taskapi.info(task['id'])

        if not task_succeeded(task):
            if task['exception'] and task['traceback']:
                raise TaskExecutionError(task['traceback'][-1])
            elif task['exception']:
                raise TaskExecutionError("Unknown metadata rebuild error: %s" %
                                         task['exception'])
            else:
                raise TaskExecutionError("Unknown metadata rebuild error")
            
        return "Metadata rebuilt for %s" % repo_id

tasks.register(RebuildMetadata)


class RebalanceSyncSchedule(Task):
    def run(self):
        errors = []
        if repo_utils.rebalance_sync_schedule(errors):
            return "Sync schedule rebalanced"
        else:
            return TaskExecutionError("Error(s) rebalancing sync schedule: %s" %
                                      ", ".join(errors))

tasks.register(RebalanceSyncSchedule)

logger.error("tasks: %s" % registry.tasks.regular().keys())
