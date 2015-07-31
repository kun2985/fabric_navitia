# -*- coding: utf-8 -*-
import StringIO
import ConfigParser
import os
from io import BytesIO
from retrying import Retrying, RetryError
import time

from fabric.api import execute, env, task
from fabric.colors import red, blue, green, yellow
from fabric.context_managers import settings, warn_only, cd, shell_env
from fabric.contrib.files import exists
from fabric.decorators import roles
from fabric.operations import run, get, sudo, put
from fabtools import require, python, files, service

from fabfile.component import db
from fabfile.component.kraken import get_no_data_instances
from fabfile import utils
from fabfile.utils import _install_packages, _upload_template, start_or_stop_with_delay


@task
@roles('tyr')
def update_tyr_conf():
    _upload_template("tyr/settings.py.jinja", env.tyr_settings_file,
                     context={
                        'env': env,
                        'tyr_broker_username': env.tyr_broker_username,
                        'tyr_broker_password': env.tyr_broker_password,
                        'rabbitmq_host': env.rabbitmq_host,
                        'rabbitmq_port': env.rabbitmq_port,
                        'tyr_postgresql_user': env.tyr_postgresql_user,
                        'tyr_postgresql_password': env.tyr_postgresql_password,
                        'postgresql_database_host': env.postgresql_database_host,
                        'tyr_postgresql_database': env.tyr_postgresql_database,
                        'tyr_base_instances_dir': env.tyr_base_instances_dir,
                        'tyr_base_logfile': env.tyr_base_logfile,
                        'redis_host': env.redis_host,
                        'redis_port': env.redis_port,
                        'tyr_redis_password': env.tyr_redis_password,
                        'tyr_redis_db': env.tyr_redis_db
                     })
    _upload_template('tyr/tyr.wsgi.jinja', env.tyr_wsgi_file,
                     context={
                         'tyr_settings_file': env.tyr_settings_file
                     })

@task
@roles('tyr')
def setup_tyr():
    require.deb.packages([
        "alembic",
        "rabbitmq-server",
        'redis-server'])
    require.users.user('www-data')

    utils.require_directories([env.tyr_base_instances_dir,
                               env.tyr_basedir,
                               ],
                              owner=env.TYR_USER,
                              group=env.TYR_USER,
                              mode='755',
                              use_sudo=True)

    utils.require_directory(env.tyr_base_logdir,
                              owner=env.TYR_USER,
                              group=env.TYR_USER,
                              mode='777',
                              use_sudo=True)

    utils.require_directory(env.tyr_base_destination_dir,
                              is_on_nfs4=False,
                              owner=env.TYR_USER,
                              group=env.TYR_USER,
                              mode='755',
                              use_sudo=True)

    require.files.file(env.tyr_base_logfile,
                              owner=env.TYR_USER,
                              group=env.TYR_USER,
                              mode='766',
                              use_sudo=True)

    require.files.file(env.tyr_logfile_pattern,
                              owner=env.TYR_USER,
                              group=env.TYR_USER,
                              mode='766',
                              use_sudo=True)

    update_tyr_conf()

    # we need to deploy the services conf file
    _upload_template('tyr/tyr_beat.jinja', env.tyr_beat_service_file, user='root', mode='755',
                     context={
                         'env': env
                     })
    _upload_template('tyr/tyr_worker.jinja', env.tyr_worker_service_file, user='root', mode='755',
                     context={
                         'env': env
                     })

    if not files.is_dir(env.tyr_migration_dir):
        files.symlink('/usr/share/tyr/migrations/', env.tyr_migration_dir, use_sudo=True)
        sudo("chown www-data:www-data {}".format(env.tyr_migration_dir))

    # we create a symlink for tyr manage_py
    tyr_symlink = os.path.join(env.tyr_basedir, 'manage.py')
    if not files.is_file(tyr_symlink):
        files.symlink('/usr/bin/manage_tyr.py', tyr_symlink, use_sudo=True)

    service.start('rabbitmq-server')
    service.start('redis-server')

@task
@roles('tyr')
def upgrade_tyr_packages():
    packages = [
        'sudo',
        'apache2',
        'libapache2-mod-wsgi',
        'logrotate',
        'python2.7',
        'git',
        'postgresql-server-dev-all'
        ]
    if env.distrib == 'ubuntu14.04':
        packages += ['libpython2.7-dev', 'postgresql-9.3-postgis-2.1']
    elif env.distrib == 'debian7':
        packages += ['python2.7-dev', 'postgresql-9.1-postgis']
    elif env.distrib == 'debian8':
        packages += ['python2.7-dev', 'g++', 'postgresql-9.4-postgis-2.1']
    require.deb.packages(packages, update=True, options=['-f'])
    package_filter_list = ['navitia-tyr*deb',
                           'navitia-common*deb']
    _install_packages(package_filter_list)
    if not python.is_pip_installed():
        python.install_pip()
    require.python.install_requirements('/usr/share/tyr/requirements.txt', use_sudo=True, exists_action='w')
    _upload_template('tyr/tyr_beat.jinja', env.tyr_beat_service_file,
                     context={'env': env}, mode='755')
    _upload_template('tyr/tyr_worker.jinja', env.tyr_worker_service_file,
                     context={'env': env}, mode='755')

@task
@roles('tyr_master')
def upgrade_db_tyr():
    with cd(env.tyr_basedir), shell_env(TYR_CONFIG_FILE=env.tyr_settings_file), settings(user=env.KRAKEN_USER):
        run('python manage.py db upgrade')

    require.service.start('tyr_beat')
    require.service.start('tyr_worker')

@task
@roles('tyr_master')
def setup_tyr_master():
    utils.require_directory(env.ed_basedir, owner='www-data', group='www-data', use_sudo=True)

@task
@roles('tyr')
def upgrade_ed_packages():
    require.deb.packages([
        'unzip',
        'python2.7',
        ])
    package_filter_list = ['navitia-ed*deb',
                           #'navitia-ed-dbg*deb',
                           'navitia-common*deb',
                           'navitia-cities*deb']
    _install_packages(package_filter_list)

    require.postgres.server()

@task
@roles('tyr_master')
def update_ed_db(instance):
    """ upgrade the instance database schema """
    if exists("%s/%s" % (env.ed_basedir, instance)):
        if env.dry_run is True:
            print("cd {env}/{instance}; PYTHONPATH=. alembic upgrade head"
                  .format(env=env.ed_basedir, instance=instance))
        else:
            with cd("{env}/{instance}".format(env=env.ed_basedir, instance=instance)):
                run("PYTHONPATH=. alembic upgrade head")
    else:
        print(red("ERROR: {env}/{instance} does not exists. skipping db update"
                  .format(env=env.ed_basedir, instance=instance)))


# TODO: testme
# @task
# def verify_tyr_dest_dir_exists(server):
#     """ Verify that the dest dir of all instances exists """
#     # first get all instances
#     execute(get_kraken_instances, server)
#
#     with settings(host_string=server):
#
#         for instance in env.kraken_instances:
#             kraken_config = get_kraken_config(server, instance)
#             target_file = kraken_config.get('GENERAL', 'database')
#
#             if not exists(target_file.replace('data.nav.lz4', '')):
#                 print(red("CRITICAL: dest dir for {} don't exists".format(instance)))


@task
@roles('tyr_master')
def stop_tyr_beat():
    start_or_stop_with_delay('tyr_beat', 4000, 500, start=False, exc_raise=True)

@task
@roles('tyr_master')
def start_tyr_beat():
    start_or_stop_with_delay('tyr_beat', 4000, 500, exc_raise=True, only_once=env.TYR_START_ONLY_ONCE)

@task
@roles('tyr_master')
def tyr_beat_status():
    sudo("service tyr_beat status")

@task
@roles('tyr')
def stop_tyr_worker():
    if not start_or_stop_with_delay('tyr_worker', delay=8000, wait=500, start=False, exc_raise=False):
        print(red("there are still tyr_worker alive, something is wrong"))
        if env.kill_ghost_tyr_worker:
            print(red('killing all workers'))

            def get_workers():
                with warn_only():
                    return run('ps -eo pid,command | grep [t]yr_worker')

            pids_to_kill = [s.split(' ', 1)[0] for s in get_workers().split('\n')]
            sudo('kill -9 {pid}'.format(pid=" ".join(pids_to_kill)))

            try:
                Retrying(stop_max_delay=4000, wait_fixed=1000,
                         retry_on_result=get_workers).call(lambda: None)
            except RetryError:
                print red('Some workers are still alive: {}'.format(get_workers()))
                print red("Aborting")
                exit(1)

@task
@roles('tyr')
def start_tyr_worker():
    if not start_or_stop_with_delay('tyr_worker', env.TYR_WORKER_START_DELAY * 1000, 500,
                                    exc_raise=False, only_once=env.TYR_START_ONLY_ONCE):
        print(red('Service tyr refuses to start!'))
        exit(1)

@task
@roles('tyr')
def restart_tyr_worker():
    stop_tyr_worker()
    start_tyr_worker()

@task
@roles('tyr_master')
def restart_tyr_beat():
    stop_tyr_beat()
    start_tyr_beat()

@task
@roles('tyr')
def start_services():
    require.postgres.server()
    require.service.started('rabbitmq-server')
    require.service.started('redis-server')

@task
@roles('tyr')
def backup_datanav(instance):
    """ Backup a data.nav.lz4 for a given instance in data.nav.lz4_$instance"""

    env.tyr_config = get_tyr_config(instance)
    kraken_db = env.tyr_config.get('instance', 'target-file')

    # if data.nav.lz4 found, copy it
    if exists("%s" % (kraken_db)):
        # nfsv4 acl, don't try to preserve permissions, inheritance do the work
        if env.standalone is False:
            run("cp %s %s_%s" % (kraken_db, kraken_db, instance))
        else:
            run("cp --archive %s %s_%s" % (kraken_db, kraken_db, instance))

        # verify backup
        print("Verify backup")
        md5_db = run("md5sum %s | awk '{print $1}'" % kraken_db)
        md5_dbcpy = run("md5sum %s_%s | awk '{print $1}'" % (kraken_db, instance))

        if md5_db != md5_dbcpy:
            print(red("%s and %s are not equals, exiting. (No space left on device ?)" % (kraken_db, kraken_db + '_' + instance)))
    else:
        print(yellow("WARNING: %s doesn't have a data.nav.lz4, add it to the auto-exclusion list for binarization" % instance))
        env.excluded_instances.append(instance)

@task
@roles('tyr')
def rollback_datanav(instance):
    """ Rollback a data.nav.lz4_$instance file for a given instance """

    env.tyr_config = get_tyr_config(instance)
    kraken_db = env.tyr_config.get('instance', 'target-file')

    # if data.nav.lz4 backup found, copy it
    if exists("%s_%s" % (kraken_db, instance)):
        # nfsv4 acl, don't try to preserve permissions, inheritance do the work
        if env.standalone is False:
            run("cp %s_%s %s" % (kraken_db, instance, kraken_db))
        else:
            run("cp --archive %s_%s %s" % (kraken_db, instance, kraken_db))

        # verify backup
        print("Verify backup")
        md5_db = run("md5sum %s | awk '{print $1}'" % kraken_db)
        md5_dbcpy = run("md5sum %s_%s | awk '{print $1}'" % (kraken_db, instance))

        if md5_db != md5_dbcpy:
            print(red("%s and %s are not equals, exiting. (No space left on device ?)" % (kraken_db, kraken_db + '_' + instance)))
    else:
        print(red("ERROR: %s_%s does not exist" % (kraken_db, instance)))

@task
@roles('tyr')
def get_tyr_config(instance):
    """ Get tyr configuration of a given instance """

    config_path = "%s/%s.ini" % (env.tyr_base_instances_dir, instance)

    # first get the configfile here
    temp_file = StringIO.StringIO()
    if exists(config_path):
        get(config_path, temp_file)
    else:
        print(red("ERROR: can't find %s" % config_path))
        exit(1)

    config = ConfigParser.RawConfigParser(allow_no_value=True)
    config.readfp(BytesIO(temp_file.getvalue()))

    if 'instance' in config.sections():
        return config
    else:
        return None

@task
@roles('tyr_master')
def launch_rebinarization_upgrade():
    """launch binarization on all instances for the upgrade"""

    # avoid any other normal binarization during upgrade
    stop_tyr_beat()
    # for each instance:
    # - upgrade the ed database
    # - binarize last processed data, blocking step (sync, which is good to
    # ensure the binarization of this instance is done)
    # - if kraken farm, disable the first engine kraken instance in lb; if
    # standalone server nothing to do
    # - restart corresponding kraken instance even if no data (to have the new
    # binary)
    # - test kraken instance via monitor-kraken
    # - only continue to next instance if instance is ok, to avoid breaking all
    # instances
    execute(get_no_data_instances)

    # tempremove already binarized instances from list
    #already_binarized_instances = ['bretagne', 'ca-qc', 'champagneardenne', 'de', 'es', 'fr-bou', 'fr-cen', 'fr-idf', 'fr-ne-amiens', 'fr-ne-c', 'fr-ne', 'fr-npdc', 'fr-nw-c', 'fr-nw', 'fr-se-c', 'fr-se', 'fr-sw-c', 'fr-sw', 'lyon', 'nl', 'paysdelaloire', 'se', 'transilien', 'us-ca']
    #for instance in already_binarized_instances:
    #   env.excluded_instances.append(instance)

    def binarize_instance(i_name):
        with utils.time_that(blue("data loaded for " + i_name + " in {elapsed}")):
            print(blue("loading data for {}".format(i_name)))
            update_ed_db(i_name)

            if i_name in env.excluded_instances:
                print(blue("NOTICE: i_name {} has been excluded, skiping it".format(i_name)))
            else:
                launch_rebinarization(i_name)

    # we run the bina in parallele (if you want sequenciel run, set env.nb_thread_for_bina = 1)
    with utils.Parallel(env.nb_thread_for_bina) as pool:
        pool.map(binarize_instance, env.instances.keys())

    start_tyr_beat()


@task
@roles('tyr_master')
def launch_rebinarization(instance):
    """ Re-launch binarization of previously processed input data
        During upgrade, we need to regenerate data.nav.lz4 file because of
        serialization objects changes; we have to find the last input file
        processed
    """
    if env.dry_run is True:
        print("DRY-RUN: cd /srv/tyr/ "
              "&& TYR_CONFIG_FILE=/srv/tyr/settings.py python manage.py import_last_dataset {i}".format(i=instance))
    else:
        with cd(env.tyr_basedir), shell_env(TYR_CONFIG_FILE=env.tyr_settings_file), settings(user=env.KRAKEN_USER):
            print(blue("NOTICE: launching binarization on {} @{}".format(instance, time.strftime('%H:%M:%S'))))
            try:
                run("python manage.py import_last_dataset {i}".format(i=instance))
            except:
                print(red("ERROR: failed binarization on {}".format(instance)))

@task
@roles('db')
def get_instance_id(instance):
    """ Return the id of a given instance """

    _upload_template("templates/db/instance_id.sql.jinja",
            "/var/lib/postgresql/postgres_{}.sql".format(instance),
            use_jinja=True,
            context={'instance': instance},
    )
    instance_id = run('su - postgres --command="psql {} --quiet --tuples-only < /var/lib/postgresql/postgres_{}.sql"'
            .format(env.jormungandr_postgresql_database, instance))
    run("rm -f /var/lib/postgresql/postgres_{}.sql".format(instance))
    return instance_id

@task
@roles('db')
def get_tyr_last_done_job_id(instance_id):
    """ Return the last done job for an instance """
    _upload_template("templates/db/last-job-instance.sql.jinja",
            "/var/lib/postgresql/postgres_{}.sql".format(instance_id),
            use_jinja=True,
            context={'instance_id': instance_id},
    )
    job_id = run('su - postgres --command="psql {} --quiet --tuples-only < /var/lib/postgresql/postgres_{}.sql"'
            .format(env.jormungandr_postgresql_database, instance_id))
    run("rm -f /var/lib/postgresql/postgres_{}.sql".format(instance_id))
    return job_id

@task
@roles('db')
def get_tyr_last_pt_data_set(instance_id):
    """Return the data_set used for """
    _upload_template("templates/db/job-id-data-set.sql.jinja",
            "/var/lib/postgresql/postgres_{}.sql".format(instance_id),
            use_jinja=True,
            context={'instance_id': instance_id},
    )
    data_set = run('su - postgres --command="psql {} --quiet --tuples-only < /var/lib/postgresql/postgres_{}.sql"'
            .format(env.jormungandr_postgresql_database, instance_id))
    run("rm -f /var/lib/postgresql/postgres_{}.sql".format(instance_id))
    return data_set


@task
@roles('tyr_master')
def test_tyr_backup_file_presence():
    """ Test if there is a binarization job done and corresponding input file exist
        This is to ensure that the binarization can use the last data_set used
    """
    return None
    # exclude instances that don't have data yet
    # execute(get_no_data_instances, env.monitor_url)

    for instance in env.instances.values():
        if instance.name in env.excluded_instances:
            print(blue("NOTICE: instance {} has been excluded, skiping it".format(instance.name)))
            continue

        instance_id = execute(get_instance_id, instance.name)

        # result is a dict where key is 'db'
        data_set = execute(get_tyr_last_pt_data_set,
                instance_id[env.roledefs['db']])[env.roledefs['db']]

        if data_set:
            # test if the file exist on the filesystem
            if not exists(data_set):
                print(red("CRITICAL: {} doesn't exist for {}".format(data_set,
                    instance.name)))
            else:
                print(green('OK: {} exist for {}'.format(data_set, instance.name)))
        else:
            print(yellow("WARNING: {} has a data.nav.lz4 but no fusio data_set job"
                "found".format(instance.name)))


@task
@roles('tyr')
def update_tyr_instance_conf(instance):
    _upload_template("tyr/instance.ini.jinja",
                     "{}/{}.ini".format(env.tyr_base_instances_dir, instance.name),
                     context={
                         'env': env,
                         'instance': instance,
                     },
    )

    # /srv/ed/$instance/alembic.ini, used by update_ed_db()
    _upload_template("tyr/ed_alembic.ini.jinja",
                     "{}/alembic.ini".format(instance.base_ed_dir),
                     context={
                         'env': env,
                         'instance': instance,
                     },
    )

    #we need a settings file to init the db with postgis
    # will be deprecated when migrating to postgis 2.1
    _upload_template("tyr/ed_settings.sh.jinja",
                     "{}/settings.sh".format(instance.base_ed_dir),
                     context={
                         'env': env,
                         'instance': instance,
                     },
    )


@task
@roles('tyr')
def create_tyr_instance(instance):
    """ Create a *private* tyr instance based on the given name
        * postgresql user + dedicated database (1 time)
        * /etc/tyr.d/instance.ini
        * create /srv/ed/<instance> + target-file basedir
    """
    # postgresql user + dedicated database
    # we create a user and a db if they does not exists
    # TODO: this is potentially executed multiple times !
    execute(db.create_instance_db, instance)

    # /srv/ed/destination/$instance & /srv/ed/backup/$instance
    utils.require_directory(instance.base_ed_dir,
                            owner=env.KRAKEN_USER, group=env.KRAKEN_USER, use_sudo=True)
    utils.require_directory(instance.source_dir,
                            owner=env.KRAKEN_USER, group=env.KRAKEN_USER, use_sudo=True)
    utils.require_directory(instance.backup_dir, is_on_nfs4=True,
                            owner=env.KRAKEN_USER, group=env.KRAKEN_USER, use_sudo=True)
    utils.require_directory(instance.base_destination_dir, is_on_nfs4=True,
                            owner=env.KRAKEN_USER, group=env.KRAKEN_USER, use_sudo=True)

    utils.require_directory(env.tyr_base_logdir,
                      owner=env.TYR_USER, group=env.TYR_USER,
                      mode='755', use_sudo=True)
    require.files.file(os.path.join(env.tyr_base_logdir, instance.name + '.log'),
                      owner=env.TYR_USER, group=env.TYR_USER,
                      mode='644', use_sudo=True)

    update_tyr_instance_conf(instance)  # Note it is not called as a task, for it needs to be done on the same server

    if not env.standalone:
        #@TODO: change user for non standalone
        pass


@task
@roles('tyr_master')
def remove_tyr_instance(instance, purge_logs=False):
    """Remove a tyr instance entirely
        * Remove ini file
        * Restart tyr worker
        * Remove tyr log
    """
    # ex.: /etc/tyr.d/fr-bou.ini
    run("rm --force %s/%s.ini" % (env.tyr_base_instances_dir, instance))
    execute(restart_tyr_worker)
    restart_tyr_beat()
    if purge_logs:
        # ex.: /var/log/tyr/northwest.log
        run("rm --force %s/%s.log" % (env.tyr_base_logdir, instance))

    # purge instance in jormungandr database
    execute(db.remove_instance_from_jormun_database, instance)

    # purge the instance database and user
    with warn_only():
        execute(db.remove_postgresql_database, db.instance2postgresql_name(instance))
        execute(db.remove_postgresql_user, db.instance2postgresql_name(instance))


@task
@roles('tyr')
def remove_sindri_instance(instance, purge_logs=False):
    """Remove a tyr instance entirely
        * Stop the service
        * Remove startup at boot time
        * Remove initscript
        * Remove configuration and pid directory and logs
    """
    run("service sindri_%s stop; sleep 1" % instance)
    # TODO: verify the service is really stopped

    run("update-rc.d -f sindri_%s remove" % instance)
    run("rm --force /etc/init.d/sindri_%s" % instance)
    run("rm --recursive --force /srv/sindri/%s/" % instance)
    if purge_logs:
        # ex.: /var/log/sindri/fr-bou.log
        run("rm --force %s/%s.log*" % (env.SINDRI_BASE_LOGDIR, instance))

@task
@roles('tyr')
def remove_at_instance(instance):
    """Remove an at / connector_rt instance entirely
        * Remove the cron
        * purge logs
    """
    # ex.: /var/log/connectors-rt/at_fr-bou
    run("rm -f %s/at_%s" % (env.AT_BASE_LOGDIR, instance))

@task
@roles('tyr')
def remove_ed_instance(instance):
    """Remove a ed instance entirely"""
    run("rm -rf %s/%s" % (env.ed_basedir, instance))
    run("rm -rf %s/%s" % (env.tyr_base_destination_dir, instance))
    run("rm -rf %s/%s" % (env.tyr_base_backup_dir, instance))


@task
@roles('tyr')
def rename_tyr_instance(current_instance, new_instance):

    # prepare the new instance name by creating all directory and files
    utils.require_directory(os.path.join(env.ed_basedir, new_instance),
                            owner=env.KRAKEN_USER, group=env.KRAKEN_USER, use_sudo=True)

    # current data.nav.lz4
    # FIXME: source dir is not the same on all platforms
    run("cp {basedir}/{current_instance}/{datanav} {basedir}/{new_instance}/"
        .format(basedir=env.ed_basedir,
                current_instance=current_instance,
                datanav=env.kraken_data_nav,
                new_instance=new_instance))


@roles('tyr')
def deploy_default_synonyms(instance):
    """
    add default synonyms to instance
    this should be done only on the first deployement
    """
    if not instance.first_deploy:
        return
    default_synonyms_file = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                        os.path.pardir, os.path.pardir,
                                        'static_files', 'ed', 'default_synonyms.txt')
    print blue("copy default synonyms for {}".format(instance.name))

    put(default_synonyms_file, instance.source_dir, use_sudo=True)
    sudo("chown {u} {f}".format(u=env.KRAKEN_USER, f=os.path.join(instance.source_dir, 'default_synonyms.txt')))