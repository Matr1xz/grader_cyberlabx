#!/usr/bin/env python3

# Instructor.py
# Description: * Read instructorlab.json and extract a zip file
#                containing the student lab work
#              * Call script to grade the student lab work

import copy
import json
from hashlib import md5
import os
import sys
import zipfile
import time
import glob
import shutil
import io
import GenReport
import Grader
import GoalsParser
import ResultParser
import UniqueCheck
import InstructorLogging
import string
import LabCount
import subprocess
import shlex

# MYHOME=os.getenv('HOME')
MYHOME = os.getcwd()
# logger = InstructorLogging.InstructorLogging("/tmp/instructor.log")
logger = InstructorLogging.InstructorLogging("./tmp/instructor.log")


class SubmissionIdentityError(ValueError):
    """Raised when submission identity metadata is inconsistent."""


def _normalize_identity(value):
    return str(value or '').strip().casefold()


def _submission_identity_parts(lab_filename):
    filename = os.path.basename(lab_filename or '')
    parts = filename.split('.')
    if len(parts) < 3 or parts[-1].lower() != 'lab':
        raise SubmissionIdentityError('submission filename must end with .lab')

    outer_email_labname = '.'.join(parts[:-1])
    lab_id = parts[-2]
    student_id = '.'.join(parts[:-2])
    if not student_id or not lab_id:
        raise SubmissionIdentityError('submission filename has no student or lab identity')
    return outer_email_labname, lab_id, student_id


def _read_nested_student_id(nested_zip):
    email_members = [
        name for name in nested_zip.namelist()
        if name.replace('\\\\', '/').lstrip('/') == '.local/.email'
    ]
    if len(email_members) != 1:
        raise SubmissionIdentityError('nested archive must contain exactly one .local/.email')

    with nested_zip.open(email_members[0], 'r') as email_file:
        student_id = email_file.read().decode('utf-8', errors='strict').strip()
    if not student_id or not all(c in string.printable for c in student_id):
        raise SubmissionIdentityError('nested .local/.email is missing or invalid')
    return student_id.replace('@', '_at_')


def validate_submission_identity(lab_filename, expected_student_id=None, expected_lab_id=None):
    """Validate outer filename, nested archive names, and embedded student ID."""
    outer_email_labname, lab_id, outer_student_id = _submission_identity_parts(lab_filename)
    if expected_student_id is not None and _normalize_identity(outer_student_id) != _normalize_identity(expected_student_id):
        raise SubmissionIdentityError('submission student identity does not match trusted identity')
    if expected_lab_id is not None and _normalize_identity(lab_id) != _normalize_identity(expected_lab_id):
        raise SubmissionIdentityError('submission lab identity does not match trusted lab')

    nested_count = 0
    with zipfile.ZipFile(lab_filename, 'r') as outer_zip:
        for info in outer_zip.infolist():
            member_name = info.filename.replace('\\\\', '/')
            if info.is_dir() or not member_name.lower().endswith('.zip'):
                continue
            nested_name = os.path.basename(member_name)
            if nested_name.lower() == 'docs.zip':
                continue
            nested_stem = nested_name[:-4]
            if '=' not in nested_stem:
                raise SubmissionIdentityError('nested archive has malformed identity name')
            nested_email_labname, container_name = nested_stem.rsplit('=', 1)
            if not nested_email_labname or not container_name:
                raise SubmissionIdentityError('nested archive has malformed identity name')
            if _normalize_identity(nested_email_labname) != _normalize_identity(outer_email_labname):
                raise SubmissionIdentityError('nested archive identity does not match outer filename')

            with outer_zip.open(info, 'r') as nested_file:
                nested_bytes = nested_file.read()
            with zipfile.ZipFile(io.BytesIO(nested_bytes), 'r') as nested_zip:
                embedded_student_id = _read_nested_student_id(nested_zip)
            expected_nested_student_id = nested_email_labname.rsplit('.', 1)[0]
            if _normalize_identity(embedded_student_id) != _normalize_identity(expected_nested_student_id):
                raise SubmissionIdentityError('embedded student identity does not match nested archive')
            if _normalize_identity(embedded_student_id) != _normalize_identity(outer_student_id):
                raise SubmissionIdentityError('embedded student identity does not match outer filename')
            nested_count += 1

    if nested_count == 0:
        raise SubmissionIdentityError('submission contains no student container archive')
    return outer_email_labname, lab_id, outer_student_id


def _safe_remove_path(path):
    if not path:
        return
    try:
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.exists(path):
            os.remove(path)
    except Exception as e:
        logger.debug('Skip cleanup path %s because %s' % (path, str(e)))


def cleanup_submission_artifacts(base_dir, email_labname, tmp_root=None):
    """
    Clean per-submission artifacts only.
    This avoids deleting shared temporary files of other concurrent requests.
    """
    if not email_labname:
        return

    tmp_root = tmp_root or os.path.join(base_dir, 'tmp')
    labtainer_dir = os.path.join(tmp_root, 'labtainer')
    labs_extracted_dir = os.path.join(tmp_root, 'labs_extracted')

    # Remove extracted tree for this submission only: tmp/labs_extracted/<email_labname>
    _safe_remove_path(os.path.join(labs_extracted_dir, email_labname))

    # Remove second-level zip remnants for this submission only: tmp/labtainer/<email_labname>=*.zip
    zip_pattern = os.path.join(labtainer_dir, '%s=*.zip' % email_labname)
    for tmpzip in glob.glob(zip_pattern):
        _safe_remove_path(tmpzip)

    # Remove first-level extracted remnants for this submission only.
    # Typical names include <email_labname>.lab, .log, .json, or subpaths starting with that prefix.
    root_patterns = [
        os.path.join(labtainer_dir, '%s=*' % email_labname),
        os.path.join(labtainer_dir, '%s.*' % email_labname),
        os.path.join(labtainer_dir, email_labname),
    ]
    for pattern in root_patterns:
        for entry in glob.glob(pattern):
            _safe_remove_path(entry)

    # Remove common leftover files that are only used during watermark checks.
    # Keep this scoped to known temp files and do not remove the whole directory.
    _safe_remove_path(os.path.join(labtainer_dir, '.local', '.email'))
    _safe_remove_path(os.path.join(labtainer_dir, '.local', '.watermark'))
    _safe_remove_path(os.path.join(labtainer_dir, '.local', '.seed'))
    _safe_remove_path(os.path.join(labtainer_dir, 'count.json'))


def build_cheated_goals(cur_lab_folder, cheated_tools):
    """
    Doc results.config va goals.config de xac dinh goal nao su dung tool bi gian lan.
    Tra ve set ten goal bi gian lan.
    """
    if not cheated_tools:
        return set()

    results_config = os.path.join(cur_lab_folder, 'instr_config', 'results.config')
    goals_config   = os.path.join(cur_lab_folder, 'instr_config', 'goals.config')
    if not os.path.isfile(results_config) or not os.path.isfile(goals_config):
        return set()

    # Buoc 1: result_id -> ten tool
    result_tool = {}
    with open(results_config, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            result_id, rest = line.split('=', 1)
            result_id = result_id.strip()
            parts = [p.strip() for p in rest.split(':')]
            if len(parts) < 2:
                continue
            # parts[0] = "container:program"
            container_prog = parts[0]
            prog = container_prog.split(':')[-1].strip()  # lay phan sau dau ':' cuoi
            if prog == '.bash_history':
                # tool = chuoi search (phan gia tri cuoi)
                if len(parts) >= 3:
                    result_tool[result_id] = parts[-1].strip().lower()
            else:
                # netstat.stdout -> netstat
                result_tool[result_id] = prog.split('.')[0].lower()

    # Buoc 2: goal_name -> cheat?
    # goals.config format: goal_name = type : [op :] result_ref [: answer=X]
    # result_ref la mot result_id tu results.config
    cheated_goals = set()
    cheated_tools_lower = {t.lower() for t in cheated_tools}
    with open(goals_config, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            goal_name, rest = line.split('=', 1)
            goal_name = goal_name.strip()
            # Kiem tra tung token xem co la result_id khong
            tokens = [t.strip() for t in rest.replace(':', ' ').split()]
            for token in tokens:
                token_lower = token.lower()
                if token_lower in result_tool and result_tool[token_lower] in cheated_tools_lower:
                    cheated_goals.add(goal_name)
                    break

    return cheated_goals


def newStudentJson():
    student_json = {}
    student_json['parameter'] = {}
    student_json['grades'] = {}
    student_json['firstlevelzip'] = {}
    student_json['secondlevelzip'] = {}
    student_json['actualwatermark'] = {}
    student_json['expectedwatermark'] = {}
    student_json['labcount'] = {}
    return student_json


def store_student_labcount(gradesjson, email_labname, student_lab_count):
    logger.debug('store_student_labcount email_labname %s' % (email_labname))
    if email_labname not in gradesjson:
        gradesjson[email_labname] = newStudentJson()
    else:
        if gradesjson[email_labname]['labcount'] != {}:
            # Already have that student's labcount stored
            logger.error("instructor.py store_student_labcount: duplicate email_labname %s labcount %s" % (
            email_labname, labcount))
            sys.exit(1)
    gradesjson[email_labname]['labcount'] = copy.deepcopy(student_lab_count)


def store_student_watermark(gradesjson, email_labname, actual_watermark, expected_watermark):
    logger.debug('store_student_watermal email_labname %s actual %s expected %s' % (
    email_labname, actual_watermark, expected_watermark))
    if email_labname not in gradesjson:
        gradesjson[email_labname] = newStudentJson()
    gradesjson[email_labname]['actualwatermark'] = actual_watermark
    gradesjson[email_labname]['expectedwatermark'] = expected_watermark


def store_student_firstlevelzip(gradesjson, email_labname, first_zip_name):
    logger.debug('store_student_firstlevelzip email_labname %s first_zip_name %s' % (email_labname, first_zip_name))
    if email_labname not in gradesjson:
        gradesjson[email_labname] = newStudentJson()
    gradesjson[email_labname]['firstlevelzip'] = first_zip_name


def store_student_secondlevelzip(gradesjson, email_labname, second_zip_name):
    logger.debug('store_student_secondlevelzip email_labname %s second_zip_name %s' % (email_labname, second_zip_name))
    if email_labname not in gradesjson:
        gradesjson[email_labname] = newStudentJson()
    gradesjson[email_labname]['secondlevelzip'] = second_zip_name


def store_student_parameter(gradesjson, email_labname, student_parameter):
    logger.debug('store_student_parameter email_labname %s student_parameter %s' % (email_labname, student_parameter))
    if email_labname not in gradesjson:
        gradesjson[email_labname] = newStudentJson()
        if gradesjson[email_labname]['parameter'] != {}:
            # Already have that student's parameter stored
            logger.error("instructor.py store_student_parameter: duplicate email_labname %s student_parameter %s" % (
            email_labname, student_parameter))
            sys.exit(1)
    gradesjson[email_labname]['parameter'] = copy.deepcopy(student_parameter)


def store_student_grades(gradesjson, email_labname, grades):
    logger.debug('store_student_grades email_labname %s grades %s' % (email_labname, grades))
    if email_labname not in gradesjson:
        gradesjson[email_labname] = newStudentJson()
        if gradesjson[email_labname]['grades'] != {}:
            # Already have that student's grades stored
            logger.error(
                "instructor.py store_student_grades: duplicate email_labname %s grades %s" % (email_labname, grades))
            sys.exit(1)
    gradesjson[email_labname]['grades'] = dict(copy.deepcopy(grades))
    # print(gradesjson[email_labname]['grades'])

def store_student_unique(uniquejson, email_labname, uniquevalues):
    logger.debug('store_student_unique email_labname %s unique %s' % (email_labname, uniquevalues))
    if email_labname not in uniquejson:
        uniquejson[email_labname] = {}
        uniquejson[email_labname]['unique'] = copy.deepcopy(uniquevalues)
    else:
        if uniquejson[email_labname]['unique'] != {}:
            # Already have that student's unique values stored
            logger.error("instructor.py store_student_unique: duplicate email_labname %s unique %s" % (
            email_labname, uniquevalues))
            sys.exit(1)
        else:
            uniquejson[email_labname]['unique'] = copy.deepcopy(uniquevalues)


# Make sure second level zip file e-mail is OK
def Check_SecondLevel_EmailWatermark_OK(gradesjson, email_labname, student_id, zipoutput, tmpdir):
    check_result = True
    TMPDIR = tmpdir
    TempEmailFile = "%s/.local/.email" % TMPDIR
    TempWatermarkFile = "%s/.local/.watermark" % TMPDIR
    TempSeedFile = "%s/.local/.seed" % TMPDIR
    # Remove Temporary Email file first then extract
    try:
        os.remove(TempEmailFile)
        os.remove(TempWatermarkFile)
        os.remove(TempSeedFile)
    except OSError:
        pass

    # Do not extract unnecessarily
    for zi in zipoutput.infolist():
        zname = zi.filename
        if zname == ".local/.email" or zname == ".local/.seed" or zname == ".local/.watermark":
            zipoutput.extract(zi, TMPDIR)

    student_id_from_file = None
    if os.path.isfile(TempEmailFile):
        with open(TempEmailFile) as fh:
            student_id_from_file = fh.read().strip().replace("@", "_at_")

    if student_id_from_file is not None:
        # Student ID obtained from zip_file_name must match the one from E-mail file
        if not all(c in string.printable for c in student_id_from_file):
            student_id_from_file = 'not_printable'
        if student_id != student_id_from_file:
            print("mismatch student_id is (%s) student_id_from_file is (%s)" % (student_id, student_id_from_file))
            store_student_secondlevelzip(gradesjson, email_labname, student_id_from_file)
            check_result = False
    else:
        print('%s missing file %s' % (email_labname, TempEmailFile))
        store_student_secondlevelzip(gradesjson, email_labname, 'No_email_file')

    if os.path.exists(TempWatermarkFile):
        with open(TempWatermarkFile) as fh:
            actual_watermark = fh.read().strip()

        # Create watermark from hash of lab_instance_seed and the watermark string
        with open(TempSeedFile) as fh:
            seed_from_file = fh.read().strip()

        the_watermark_string = "LABTAINER_WATERMARK1"
        string_to_be_hashed = '%s:%s' % (seed_from_file, the_watermark_string)
        mymd5 = md5()
        mymd5.update(string_to_be_hashed.encode('utf-8'))
        expected_watermark = mymd5.hexdigest()
        # print expected_watermark

        # Watermark must match
        if actual_watermark != expected_watermark:
            # print "mismatch actual is (%s) expected is (%s)" % (actual_watermark, expected_watermark)
            check_result = False
        # Store the actual and expected watermark regardless
        # So that when generating report, we can figure out the 'source'
        store_student_watermark(gradesjson, email_labname, actual_watermark, expected_watermark)

    return check_result


# Usage: instructor_grade.py <lab file from a student to grade>
# Arguments:
#     <lab file from a student to grade> - This is the input file <email>.<lab_id>.lab
# i.e: anhlh.B18DCAT005_at_stu.ptit.edu.vn.gdblesson.lab

# Chuan bi: copy thu muc pregrade moi nhat vao thu muc .local, sau do chu y cac thu muc phu nhu tmp, labs, labtainer, lab_extracted
# Mot so file thu vien trong thu muc labtainer-student/bin va labtainer-student/lab-bin

def instructor_grade_lab(lab_filename, expected_student_id=None, expected_lab_id=None):
    logger.info("Begin logging instructor_grade_lab function in instructor_grade.py")
    try:
        validate_submission_identity(
            lab_filename,
            expected_student_id=expected_student_id,
            expected_lab_id=expected_lab_id,
        )
    except (OSError, ValueError, zipfile.BadZipFile, SubmissionIdentityError) as exc:
        logger.warning('Rejected submission identity for %s: %s' % (lab_filename, exc))
        return {
            'error': 'invalid_submission_identity',
            'detail': str(exc),
        }
    # Enable watermark-based integrity checks.
    check_watermark = True
    logger.debug('MYHOME is %s' % MYHOME)
    current_folder = os.getcwd()
    logger.info('Current folder is %s' % current_folder)
    # Xac dinh ten lab dua tren ten file
    lab_name_base = os.path.basename(lab_filename) #b25dcat057.tcpip.lab
    lab_name_base_list = lab_name_base.split('.')   #b25dcat057.tcpip.lab -> ['b25dcat057', 'tcpip', 'lab']
    # Nhan ten lab thong qua lab_id_name
    lab_id_name = lab_name_base_list[-2] # tcpip
    lab_name_email = '.'.join(lab_name_base_list[:-2]) # b25dcat057
    lab_file_ext = lab_name_base_list[-1] # lab
    #Lay danh sach lab trong thu muc pregrade
    PREGRADE_FOLDER = os.path.normpath(os.path.join(current_folder, '.local', 'pregrade'))
    CUR_LAB_FOLDER = os.path.normpath(os.path.join(PREGRADE_FOLDER, lab_id_name))
    lab_list = [item for item in os.listdir(PREGRADE_FOLDER) if os.path.isdir(os.path.join(PREGRADE_FOLDER, item))] #[lab1, lab2, ...]
    if lab_id_name not in lab_list or lab_file_ext !='lab':
        logger.error('The input file is wrong 12345!')
        # sys.exit(1)
        return 'wrong_input_file'

    # lab_filename is expected to live in a per-request workspace folder.
    lab_parent_dir = os.path.dirname(os.path.abspath(lab_filename))
    TMPDIR = os.path.normpath(os.path.join(lab_parent_dir, 'labtainer'))
    LAB_EXTRACTED_FOLDER = os.path.normpath(os.path.join(lab_parent_dir, 'labs_extracted'))
    checkwork_arg = None
    checkwork = False
    check_watermark = True
    # if len(sys.argv) > 1:
    #     checkwork_arg = str(sys.argv[1]).upper()
    #
    #     if checkwork_arg == "TRUE":
    #         check_watermark = False
    #         checkwork = True

    # is this used?
    InstructorBaseDir = os.path.join(MYHOME, '.local', 'base') # MYHOME/.local/base

    ''' dictionary of container lists keyed by student email_labname '''
    student_list = {}

    # Store grades, goals, etc
    gradesjson = {}
    # Store Unique checks, etc
    uniquejson = {}

    # Ensure temporary directory exists.
    if os.path.exists(TMPDIR):
        # exists but is not a directory
        if not os.path.isdir(TMPDIR):
            # remove file then create directory
            os.remove(TMPDIR)
            os.makedirs(TMPDIR)
    else:
        # does not exists, create directory
        os.makedirs(TMPDIR)

    # ''' unzip everything '''
    # ''' First level unzip '''
    # zip_files = glob.glob(LAB_FOLDER + '/*.zip')
    # lab_files = glob.glob(LAB_FOLDER + '/*.lab')
    # zip_files.extend(lab_files)
    first_level_zip = []
    zip_file_name = os.path.basename(lab_filename) #b25dcat057.tcpip.lab
    orig_email_labname, orig_zipext = zip_file_name.rsplit('.', 1) # b25dcat057.tcpip

    # Clean previous remnants for this exact submission only.
    cleanup_submission_artifacts(MYHOME, orig_email_labname, tmp_root=lab_parent_dir)

    first_level_zip.append(zip_file_name) # first_level_zip = [b25dcat057.tcpip.lab]
    OutputName = os.path.normpath(lab_filename)
    zipoutput = zipfile.ZipFile(OutputName, "r") # mo file zip de extract vao thu muc tmp/labtainer
    ''' retain dates of student files '''
    for zi in zipoutput.infolist(): # duyet tung file trong zip
        zname = zi.filename
        if not (zname.endswith('.zip') or \
                zname.endswith('.log') or \
                zname.endswith('.json')):
            continue
        if '=' in zname: # Neu ten file co chua dau '=', thi coi nhu la ten file zip cap 2, va lay phan truoc dau '=' lam email_labname
            second_email_labname, second_containername = zname.rsplit('=', 1)
            # Mismatch e-mail name at first level
            if orig_email_labname != second_email_labname: # chong RCE
                store_student_firstlevelzip(gradesjson, orig_email_labname, second_email_labname)
                # DO NOT process that student's zip file any further, i.e., DO NOT extract
                print('DO NOT process that students zip file any further, i.e., DO NOT extract')
                continue
        zipoutput.extract(zi, TMPDIR) # extract file zip vao thu muc tmp/labtainer , nhu kieu la extract lv1
        date_time = time.mktime(zi.date_time + (0, 0, -1))
        dest = os.path.join(TMPDIR, zi.filename)
        os.utime(dest, (date_time, date_time))
    zipoutput.close()

    # Add docs.zip as a file to skip also
    first_level_zip.append('docs.zip')

    ''' Second level unzip '''
    zip_files = glob.glob(TMPDIR + '/*.zip') # toan bo file zip trong /tmp/labtainer sau khi da extract cap 1, chuan bi extract cap 2
    for zfile in zip_files:
        zip_file_name = os.path.basename(zfile) # ten file zip cap 2, vd: b25dcat057.tcpip.lab.zip
        # Skip first level zip files
        if zip_file_name in first_level_zip:
            continue
        # print('zipfile is %s' % zip_file_name)
        DestinationDirName = os.path.splitext(zip_file_name)[0] # ten file zip cap 2 khong chua duoi .zip, vd: b25dcat057.tcpip=tcpip.attacker.student
        if '=' in DestinationDirName:
            # NOTE: New format has DestinationDirName as:
            #       e-mail+labname '=' containername
            # get email_labname and containername
            email_labname, containername = DestinationDirName.rsplit('=', 1) #email_labname = b25dcat057.tcpip, containername = tcpip.attacker.student
            # Replace the '=' to '/'
            DestinationDirName = '%s/%s' % (email_labname, containername) # DestinationDirName = b25dcat057.tcpip/tcpip.attacker.student
            # print email_labname
        else:
            # Old format - no containername
            logger.error("Instructor.py old format (no containername) no longer supported!\n")
            return 1
        student_id = email_labname.rsplit('.', 1)[0] # b25dcat057
        # print "student_id is %s" % student_id
        logger.debug("student_id is %s" % student_id)
        OutputName = '%s/%s' % (TMPDIR, zip_file_name) # OutputName = /tmp/labtainer/b25dcat057.tcpip.lab.zip
        # lab_dir_name = os.path.join(MYHOME, email_labname)
        # DestDirName = os.path.join(MYHOME, DestinationDirName)
        lab_dir_name = os.path.normpath(os.path.join(LAB_EXTRACTED_FOLDER, email_labname)) # lab_dir_name = MYHOME/tmp/labs_extracted/b25dcat057.tcpip
        DestDirName = os.path.normpath(os.path.join(LAB_EXTRACTED_FOLDER, DestinationDirName)) # DestDirName = MYHOME/tmp/labs_extracted/b25dcat057.tcpip/tcpip.attacker.student
        InstDirName = os.path.join(InstructorBaseDir, DestinationDirName) # InstDirName = MYHOME/.local/base/b25dcat057.tcpip/tcpip.attacker.student
        src_count_path = os.path.join(TMPDIR, 'count.json') # src_count_path = /tmp/labtainer/count.json, file count.json duoc tao ra sau khi extract cap 1, chua extract cap 2, file count.json chua thong tin so lan lab da duoc chay tren container cua sinh vien
        dst_count_path = LabCount.getPath(lab_dir_name, lab_id_name) # dst_count_path = MYHOME/tmp/labs_extracted/b25dcat057.tcpip/count.json, file count.json duoc copy vao thu muc lab_extracted sau khi extract cap 2, file count.json chua thong tin so lan lab da duoc chay tren container cua sinh vien
        # print('dst_count_path is %s' % dst_count_path)
        if os.path.isfile(src_count_path):
            #  ad-hoc fix to remnants of old bug, remove this
            if os.path.isdir(dst_count_path):
                logger.debug('removing errored directory %s' % dst_count_path)
                print('removing errored directory %s' % dst_count_path)
                shutil.rmtree(dst_count_path)
            parent = os.path.dirname(dst_count_path)
            # print('parent %s' % parent)
            try:
                os.makedirs(parent)
            except:
                pass
            # print('found count.json')
            shutil.copyfile(src_count_path, dst_count_path)

        # print "Student Lab list : "
        # print studentslablist

        if os.path.exists(DestDirName):
            # print "Removing %s" % DestDirName
            _safe_remove_path(DestDirName)

        zipoutput = zipfile.ZipFile(OutputName, "r") # mo file zip cap 2 de extract vao thu muc tmp/labtainer , nhu kieu la extract lv2

        # Do Watermark checks only if check_watermark is True
        if check_watermark:
            # If e-mail mismatch, do not further extract the zip file
            if not Check_SecondLevel_EmailWatermark_OK(gradesjson, email_labname, student_id, zipoutput, TMPDIR):
                # continue with next one
                continue

        # If no problem with e-mail, then continue processing
        if email_labname not in student_list:
            student_list[email_labname] = []
        student_list[email_labname].append(containername) # student_list = {b25dcat057.tcpip: [tcpip.attacker.student, ...], ...}
        # print('append container %s for student %s' % (containername, email_labname))
        logger.debug('append container %s for student %s' % (containername, email_labname))

        ''' retain dates of student files '''
        for zi in zipoutput.infolist():
            zipoutput.extract(zi, DestDirName)
            date_time = time.mktime(zi.date_time + (0, 0, -1))
            dest = os.path.join(DestDirName, zi.filename)
            os.utime(dest, (date_time, date_time))

        zipoutput.close()

    # pregrade_script = os.path.join(MYHOME,'.local','instr_config', 'pregrade.sh')
    pregrade_script = os.path.normpath(CUR_LAB_FOLDER + '/instr_config/pregrade.sh') # pregrade_script = MYHOME/.local/pregrade/lab_id_name/instr_config/pregrade.sh

    do_pregrade = False

    if os.path.isfile(pregrade_script):
        do_pregrade = True

    # 25/3/2023
    # Bỏ qua chạy pregrade_script
    do_pregrade = False

    # unique_check = os.path.join(MYHOME,'.local','instr_config', 'unique.config')
    unique_check = os.path.normpath(CUR_LAB_FOLDER + '/instr_config/unique.config')
    do_unique = False
    if os.path.isfile(unique_check):
        do_unique = True
    ''' create per-student goals.json and process results for each student '''
    for email_labname in student_list:
        # GoalsParser is now tied per student - do this after unzipping file
        # Call GoalsParser script to parse 'goals'
        ''' note odd hack, labinstance seed is stored on container, so need to fine one, use first '''
        DestinationDirName = '%s/%s' % (email_labname, student_list[email_labname][0])
        DestDirName = os.path.normpath(os.path.join(LAB_EXTRACTED_FOLDER, DestinationDirName))
        # TBD also getting what, student parameters from first container.
        # Better way to get instr_config files than do duplicate on each container?  Just put on grader?
        # student_parameter = GoalsParser.ParseGoals(MYHOME, DestDirName, logger)
        # CUR_LAB_FOLDER = os.path.join(MYHOME, '.local', 'pregrade', lab_id_name)
        student_parameter = GoalsParser.ParseGoals(CUR_LAB_FOLDER, DestDirName, logger)

        if student_parameter is None:
            print('Could not grade %s, skipping' % email_labname)
            continue

        #TASK: can check lai cac tham so nay la gi
        for param in student_parameter:
            env_var = 'LABTAINER_%s' % param
            os.environ[env_var] = student_parameter[param]

        if do_pregrade:
            ''' invoke pregrade for each container '''
            for container in student_list[email_labname]:
                dest = os.path.join(email_labname, container)
                cmd = '%s %s %s' % (pregrade_script, MYHOME, dest)
                logger.debug('invoke pregrade script %s' % cmd)
                ps = subprocess.Popen(shlex.split(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                output = ps.communicate()
                if len(output[1]) > 0:
                    logger.debug('command was %s' % cmd)
                    logger.debug(output[1].decode('utf-8'))

        ''' backward compatible for test sets '''
        for container in student_list[email_labname]:
            dest = os.path.join(email_labname, container)
            look_for = os.path.normpath(dest + '/.local/result/checklocal*')
            check_local_list = glob.glob(look_for)
            for cl in check_local_list:
                newname = cl.replace('checklocal', 'precheck')
                shutil.move(cl, newname)

        # Call ResultParser script to parse students' result
        # lab_dir_name = os.path.join(MYHOME, email_labname)
        # print('call ResultParser for %s %s' % (email_labname, student_list[email_labname]))
        logger.debug('call ResultParser for %s %s' % (email_labname, student_list[email_labname]))
        ResultParser.ParseStdinStdout(CUR_LAB_FOLDER, lab_dir_name, student_list[email_labname], InstDirName,
                                      lab_id_name, logger)
        # = os.path.join(MYHOME, '.local', 'pregrade', lab_id_name)
        # Add student's parameter
        store_student_parameter(gradesjson, email_labname, student_parameter)

        if do_unique:
            # print('call UniqueCheck for %s %s' % (email_labname, student_list[email_labname]))
            logger.debug('call UniqueCheck for %s %s' % (email_labname, student_list[email_labname]))
            uniquevalues = UniqueCheck.UniqueCheck(MYHOME, lab_dir_name, student_list[email_labname], InstDirName,
                                                   lab_id_name, logger)
            # Add student's unique check
            store_student_unique(uniquejson, email_labname, uniquevalues)

    ''' assess the results and generate simple report '''
    for email_labname in student_list:
        # lab_dir_name = os.path.join(MYHOME, email_labname)
        lab_dir_name = os.path.normpath(os.path.join(LAB_EXTRACTED_FOLDER, email_labname))

        grades = Grader.ProcessStudentLab(lab_dir_name, lab_id_name, logger)
        student_id = email_labname.rsplit('.', 1)[0]
        LabIDStudentName = '%s : %s : ' % (lab_id_name, student_id)

        # Add student's grades
        store_student_grades(gradesjson, email_labname, grades)

        # Add student's lab counter (if exists)
        student_lab_count = LabCount.getLabCount(lab_dir_name, lab_id_name, logger)
        store_student_labcount(gradesjson, email_labname, student_lab_count)

    return gradesjson


def main():
    #print "instructor_grade.py"
    if len(sys.argv) != 2:
        sys.stderr.write("Usage: instructor_grade.py <lab file>\n")
        # return 1
        print('Default lab is %s ' % r'D:\PycharmProjects\chamdiem\Labtainers\scripts\labtainer-instructor\flask\tmp\labs\hadv.b18at065_at_stu.ptit.edu.vn.gdblesson.lab')
        lab_filename = r'D:\PycharmProjects\chamdiem\Labtainers\scripts\labtainer-instructor\flask\tmp\labs\hadv.b18at065_at_stu.ptit.edu.vn.gdblesson.lab'
    else:
        lab_filename = sys.argv[1]

    logger.info("Begin logging instructor_grade.py")

    # # Default to check_watermark to True
    # check_watermark = True
    # logger.debug('MYHOME is %s' % MYHOME)
    # current_folder = os.getcwd()
    # logger.info('Current folder is %s' % current_folder)
    # # Xac dinh ten lab dua tren ten file
    # lab_name_base = os.path.basename(lab_filename)
    # lab_name_base_list = lab_name_base.split('.')
    # # Nhan ten lab thong qua lab_id_name
    # lab_id_name = lab_name_base_list[-2]
    # lab_name_email = '.'.join(lab_name_base_list[:-2])
    # lab_file_ext = lab_name_base_list[-1]
    # #Lay danh sach lab trong thu muc pregrade
    # PREGRADE_FOLDER = os.path.normpath(os.path.join(current_folder, '.local', 'pregrade'))
    # CUR_LAB_FOLDER = os.path.normpath(os.path.join(PREGRADE_FOLDER, lab_id_name))
    # lab_list = [item for item in os.listdir(PREGRADE_FOLDER) if os.path.isdir(os.path.join(PREGRADE_FOLDER, item))]
    # if lab_id_name not in lab_list or lab_file_ext !='lab':
    #     logger.error('The input file is wrong!')
    #     sys.exit(1)
    #
    # LAB_FOLDER = os.path.normpath(MYHOME + '/tmp/labs')
    # TMPDIR = os.path.normpath(MYHOME + '/tmp/labtainer')
    # LAB_EXTRACTED_FOLDER = os.path.normpath(MYHOME + '/tmp/labs_extracted')
    # checkwork_arg = None
    # checkwork = False
    # check_watermark = True
    # # if len(sys.argv) > 1:
    # #     checkwork_arg = str(sys.argv[1]).upper()
    # #
    # #     if checkwork_arg == "TRUE":
    # #         check_watermark = False
    # #         checkwork = True
    #
    # # is this used?
    # InstructorBaseDir = os.path.join(MYHOME, '.local', 'base')
    #
    # ''' dictionary of container lists keyed by student email_labname '''
    # student_list = {}
    #
    # # Store grades, goals, etc
    # gradesjson = {}
    # # Store Unique checks, etc
    # uniquejson = {}
    #
    # ''' remove zip files in /tmp/labtainer directory '''
    # # /tmp/labtainer will be used to store temporary zip files
    # # TMPDIR = "/tmp/labtainer"
    # if os.path.exists(TMPDIR):
    #     # exists but is not a directory
    #     if not os.path.isdir(TMPDIR):
    #         # remove file then create directory
    #         os.remove(TMPDIR)
    #         os.makedirs(TMPDIR)
    # else:
    #     # does not exists, create directory
    #     os.makedirs(TMPDIR)
    # for tmpzip in glob.glob("%s/*.zip" % TMPDIR):
    #     os.remove(tmpzip)
    #
    # # ''' unzip everything '''
    # # ''' First level unzip '''
    # # zip_files = glob.glob(LAB_FOLDER + '/*.zip')
    # # lab_files = glob.glob(LAB_FOLDER + '/*.lab')
    # # zip_files.extend(lab_files)
    # first_level_zip = []
    # zip_file_name = os.path.basename(lab_filename)
    # orig_email_labname, orig_zipext = zip_file_name.rsplit('.', 1)
    # first_level_zip.append(zip_file_name)
    # OutputName = os.path.join(LAB_FOLDER, zip_file_name)
    # zipoutput = zipfile.ZipFile(OutputName, "r")
    # ''' retain dates of student files '''
    # for zi in zipoutput.infolist():
    #     zname = zi.filename
    #     if not (zname.endswith('.zip') or \
    #             zname.endswith('.log') or \
    #             zname.endswith('.json')):
    #         continue
    #     if '=' in zname:
    #         second_email_labname, second_containername = zname.rsplit('=', 1)
    #         # Mismatch e-mail name at first level
    #         if orig_email_labname != second_email_labname:
    #             store_student_firstlevelzip(gradesjson, orig_email_labname, second_email_labname)
    #             # DO NOT process that student's zip file any further, i.e., DO NOT extract
    #             print('DO NOT process that students zip file any further, i.e., DO NOT extract')
    #             continue
    #     zipoutput.extract(zi, TMPDIR)
    #     date_time = time.mktime(zi.date_time + (0, 0, -1))
    #     dest = os.path.join(TMPDIR, zi.filename)
    #     os.utime(dest, (date_time, date_time))
    # zipoutput.close()
    #
    # # Add docs.zip as a file to skip also
    # first_level_zip.append('docs.zip')
    #
    # ''' Second level unzip '''
    # zip_files = glob.glob(TMPDIR + '/*.zip')
    # for zfile in zip_files:
    #     zip_file_name = os.path.basename(zfile)
    #     # Skip first level zip files
    #     if zip_file_name in first_level_zip:
    #         continue
    #     # print('zipfile is %s' % zip_file_name)
    #     DestinationDirName = os.path.splitext(zip_file_name)[0]
    #     if '=' in DestinationDirName:
    #         # NOTE: New format has DestinationDirName as:
    #         #       e-mail+labname '=' containername
    #         # get email_labname and containername
    #         email_labname, containername = DestinationDirName.rsplit('=', 1)
    #         # Replace the '=' to '/'
    #         DestinationDirName = '%s/%s' % (email_labname, containername)
    #         # print email_labname
    #     else:
    #         # Old format - no containername
    #         logger.error("Instructor.py old format (no containername) no longer supported!\n")
    #         return 1
    #     student_id = email_labname.rsplit('.', 1)[0]
    #     # print "student_id is %s" % student_id
    #     logger.debug("student_id is %s" % student_id)
    #     OutputName = '%s/%s' % (TMPDIR, zip_file_name)
    #     # lab_dir_name = os.path.join(MYHOME, email_labname)
    #     # DestDirName = os.path.join(MYHOME, DestinationDirName)
    #     lab_dir_name = os.path.normpath(os.path.join(LAB_EXTRACTED_FOLDER, email_labname))
    #     DestDirName = os.path.normpath(os.path.join(LAB_EXTRACTED_FOLDER, DestinationDirName))
    #     InstDirName = os.path.join(InstructorBaseDir, DestinationDirName)
    #     src_count_path = os.path.join(TMPDIR, 'count.json')
    #     dst_count_path = LabCount.getPath(lab_dir_name, lab_id_name)
    #     # print('dst_count_path is %s' % dst_count_path)
    #     if os.path.isfile(src_count_path):
    #         #  ad-hoc fix to remnants of old bug, remove this
    #         if os.path.isdir(dst_count_path):
    #             logger.debug('removing errored directory %s' % dst_count_path)
    #             print('removing errored directory %s' % dst_count_path)
    #             shutil.rmtree(dst_count_path)
    #         parent = os.path.dirname(dst_count_path)
    #         # print('parent %s' % parent)
    #         try:
    #             os.makedirs(parent)
    #         except:
    #             pass
    #         # print('found count.json')
    #         shutil.copyfile(src_count_path, dst_count_path)
    #
    #     # print "Student Lab list : "
    #     # print studentslablist
    #
    #     if os.path.exists(DestDirName):
    #         # print "Removing %s" % DestDirName
    #         os.system('rm -rf %s' % DestDirName)
    #
    #     zipoutput = zipfile.ZipFile(OutputName, "r")
    #
    #     # Do Watermark checks only if check_watermark is True
    #     if check_watermark:
    #         # If e-mail mismatch, do not further extract the zip file
    #         if not Check_SecondLevel_EmailWatermark_OK(gradesjson, email_labname, student_id, zipoutput):
    #             # continue with next one
    #             continue
    #
    #     # If no problem with e-mail, then continue processing
    #     if email_labname not in student_list:
    #         student_list[email_labname] = []
    #     student_list[email_labname].append(containername)
    #     # print('append container %s for student %s' % (containername, email_labname))
    #     logger.debug('append container %s for student %s' % (containername, email_labname))
    #
    #     ''' retain dates of student files '''
    #     for zi in zipoutput.infolist():
    #         zipoutput.extract(zi, DestDirName)
    #         date_time = time.mktime(zi.date_time + (0, 0, -1))
    #         dest = os.path.join(DestDirName, zi.filename)
    #         os.utime(dest, (date_time, date_time))
    #
    #     zipoutput.close()
    #
    # # pregrade_script = os.path.join(MYHOME,'.local','instr_config', 'pregrade.sh')
    # pregrade_script = os.path.normpath(CUR_LAB_FOLDER + '/instr_config/pregrade.sh')
    #
    # do_pregrade = False
    # if os.path.isfile(pregrade_script):
    #     do_pregrade = True
    # # unique_check = os.path.join(MYHOME,'.local','instr_config', 'unique.config')
    # unique_check = os.path.normpath(CUR_LAB_FOLDER + '/instr_config/unique.config')
    # do_unique = False
    # if os.path.isfile(unique_check):
    #     do_unique = True
    # ''' create per-student goals.json and process results for each student '''
    # for email_labname in student_list:
    #     # GoalsParser is now tied per student - do this after unzipping file
    #     # Call GoalsParser script to parse 'goals'
    #     ''' note odd hack, labinstance seed is stored on container, so need to fine one, use first '''
    #     DestinationDirName = '%s/%s' % (email_labname, student_list[email_labname][0])
    #     DestDirName = os.path.normpath(os.path.join(LAB_EXTRACTED_FOLDER, DestinationDirName))
    #     # TBD also getting what, student parameters from first container.
    #     # Better way to get instr_config files than do duplicate on each container?  Just put on grader?
    #     # student_parameter = GoalsParser.ParseGoals(MYHOME, DestDirName, logger)
    #     # CUR_LAB_FOLDER = os.path.join(MYHOME, '.local', 'pregrade', lab_id_name)
    #     student_parameter = GoalsParser.ParseGoals(CUR_LAB_FOLDER, DestDirName, logger)
    #
    #     if student_parameter is None:
    #         print('Could not grade %s, skipping' % email_labname)
    #         continue
    #
    #     #TASK: can check lai cac tham so nay la gi
    #     for param in student_parameter:
    #         env_var = 'LABTAINER_%s' % param
    #         os.environ[env_var] = student_parameter[param]
    #
    #     if do_pregrade:
    #         ''' invoke pregrade for each container '''
    #         for container in student_list[email_labname]:
    #             dest = os.path.join(email_labname, container)
    #             cmd = '%s %s %s' % (pregrade_script, MYHOME, dest)
    #             logger.debug('invoke pregrade script %s' % cmd)
    #             ps = subprocess.Popen(shlex.split(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    #             output = ps.communicate()
    #             if len(output[1]) > 0:
    #                 logger.debug('command was %s' % cmd)
    #                 logger.debug(output[1].decode('utf-8'))
    #
    #     ''' backward compatible for test sets '''
    #     for container in student_list[email_labname]:
    #         dest = os.path.join(email_labname, container)
    #         look_for = os.path.normpath(dest + '/.local/result/checklocal*')
    #         check_local_list = glob.glob(look_for)
    #         for cl in check_local_list:
    #             newname = cl.replace('checklocal', 'precheck')
    #             shutil.move(cl, newname)
    #
    #     # Call ResultParser script to parse students' result
    #     # lab_dir_name = os.path.join(MYHOME, email_labname)
    #     # print('call ResultParser for %s %s' % (email_labname, student_list[email_labname]))
    #     logger.debug('call ResultParser for %s %s' % (email_labname, student_list[email_labname]))
    #     ResultParser.ParseStdinStdout(CUR_LAB_FOLDER, lab_dir_name, student_list[email_labname], InstDirName,
    #                                   lab_id_name, logger)
    #     # = os.path.join(MYHOME, '.local', 'pregrade', lab_id_name)
    #     # Add student's parameter
    #     store_student_parameter(gradesjson, email_labname, student_parameter)
    #
    #     if do_unique:
    #         # print('call UniqueCheck for %s %s' % (email_labname, student_list[email_labname]))
    #         logger.debug('call UniqueCheck for %s %s' % (email_labname, student_list[email_labname]))
    #         uniquevalues = UniqueCheck.UniqueCheck(MYHOME, lab_dir_name, student_list[email_labname], InstDirName,
    #                                                lab_id_name, logger)
    #         # Add student's unique check
    #         store_student_unique(uniquejson, email_labname, uniquevalues)
    #
    # ''' assess the results and generate simple report '''
    # for email_labname in student_list:
    #     # lab_dir_name = os.path.join(MYHOME, email_labname)
    #     lab_dir_name = os.path.normpath(os.path.join(LAB_EXTRACTED_FOLDER, email_labname))
    #
    #     grades = Grader.ProcessStudentLab(lab_dir_name, lab_id_name, logger)
    #     student_id = email_labname.rsplit('.', 1)[0]
    #     LabIDStudentName = '%s : %s : ' % (lab_id_name, student_id)
    #
    #     # Add student's grades
    #     store_student_grades(gradesjson, email_labname, grades)
    #
    #     # Add student's lab counter (if exists)
    #     student_lab_count = LabCount.getLabCount(lab_dir_name, lab_id_name, logger)
    #     store_student_labcount(gradesjson, email_labname, student_lab_count)
    #

    gradesjson = instructor_grade_lab(lab_filename)

    # print "grades (in JSON) is "
    # print gradesjson

    # Output <labname>.grades.json
    # gradesjsonname = os.path.join(MYHOME, "%s.grades.json" % lab_id_name)
    gradesjsonname = os.path.join(MYHOME, "grades.%s.json" % os.path.basename(lab_filename))
    gradesjsonoutput = open(gradesjsonname, "w")
    try:
        jsondumpsoutput = json.dumps(gradesjson, indent=4)
    except:
        print('json dumps failed on %s' % gradesjson)
        exit(1)
    # print('dumping %s' % str(jsondumpsoutput))
    gradesjsonoutput.write(jsondumpsoutput)
    gradesjsonoutput.write('\n')
    gradesjsonoutput.close()

    # if do_unique:
    #     # Output <labname>.unique.json
    #     # uniquejsonname = os.path.join(MYHOME, "%s.unique.json" % lab_id_name)
    #     uniquejsonname = os.path.join(lab_dir_name, "%s.unique.json" % lab_id_name)
    #     uniquejsonoutput = open(uniquejsonname, "w")
    #     try:
    #         jsondumpsoutput = json.dumps(uniquejson, indent=4)
    #     except:
    #         print('json dumps failed on %s' % uniquejson)
    #         exit(1)
    #     # print('dumping %s' % str(jsondumpsoutput))
    #     uniquejsonoutput.write(jsondumpsoutput)
    #     uniquejsonoutput.write('\n')
    #     uniquejsonoutput.close()

    # Output <labname>.grades.txt
    # gradestxtname = os.path.join(MYHOME, "%s.grades.txt" % lab_id_name)
    # gradestxtname = os.path.join(MYHOME, "log.grades.txt")
    # gradestxtname = os.path.join(CUR_LAB_FOLDER, "%s.grades.txt" % lab_id_name)
    # GenReport.CreateReport(gradesjsonname, gradestxtname, check_watermark, checkwork, CUR_LAB_FOLDER)
    # if do_unique:
    #     GenReport.UniqueReport(uniquejsonname, gradestxtname)

    # Inform user where the 'grades.txt' are created
    print("Grades are stored in '%s'" % gradesjsonname)
    return 0

if __name__ == '__main__':
    sys.exit(main())
