from lib2to3.pgen2.token import INDENT
from django.http import response, FileResponse
from django.shortcuts import render, HttpResponse, redirect
import json
from django.db import connection
from taicat.models import Deployment, GeoStat, HomePageStat, Image, Contact, Organization, Project, Species, StudyAreaStat, ProjectMember,ParameterCode, ProjectStat, StudyArea
from django.db.models import Count, Window, F, Sum, Min, Q, Max, DateTimeField, ExpressionWrapper
from django.db.models.functions import ExtractYear
from django.template import loader
from django.core.paginator import Paginator
from django.core.cache import cache
from django.contrib import messages
from django.core.files.storage import FileSystemStorage
from django.utils import timezone
from conf.settings import BASE_DIR
from shapely.geometry import Point, shape

import requests
import logging

import time
import pandas as pd
from datetime import datetime, timedelta

import os
from django.conf import settings

logger = logging.getLogger(__name__)
# from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.core.mail import EmailMessage
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
import threading
from django.http import response, JsonResponse
from .models import *
from taicat.utils import get_my_project_list, get_project_member, get_studyarea_member,get_none_studyarea_project_member
from django.db.models.functions import Trunc, TruncDate
from .utils import (
    DecimalEncoder,
    update_studyareastat,
    get_request_site_url,
    session_login_required)
from django.views.decorators.csrf import csrf_exempt
# from django.core import serializers
import geopandas as gpd
from shapely.geometry import Point


def admin_dashboard(request):
    if not request.user.is_authenticated:
        return redirect('/admin/login/?next=/admin-dashboard')

    context = {}

    if 0: #cache
        pass
    #ctx := cache.get('dashboard_context'):
    #context = json.loads(ctx)
    else:
        sql = '''
SELECT
  r.project_id,
  r.project_name,
  r.num_annotations,
  r.num_images,
  r.last_upload_time,
  r.first_datetime,
  r.last_datetime,
  sa.num_studyareas,
  dep.num_deployments,
  dj.num_uploads,
  sp.num_species
FROM
  ( SELECT
      i.project_id as project_id,
      p.name AS project_name,
      COUNT(*) AS num_annotations,
      COUNT(1) FILTER (WHERE i.annotation_seq=0) AS num_images,
      TIMEZONE('Asia/Taipei', MAX(i.created)) AS last_upload_time,
      TIMEZONE('Asia/Taipei', MAX(i.datetime)) AS last_datetime,
      TIMEZONE('Asia/Taipei', MIN(i.datetime)) AS first_datetime
    FROM taicat_image AS i
    JOIN taicat_project AS p ON p.id = i.project_id
    GROUP BY i.project_id, p.name
  ) AS r
LEFT JOIN (
  SELECT project_id, count(*) AS num_studyareas FROM taicat_studyarea GROUP BY project_id
  ) AS sa ON sa.project_id = r.project_id
LEFT JOIN (
  SELECT project_id, count(*) AS num_deployments FROM taicat_deployment GROUP BY project_id
  ) AS dep ON dep.project_id = r.project_id
LEFT JOIN (
  SELECT project_id, count(*) AS num_uploads FROM taicat_deploymentjournal GROUP BY project_id
  ) AS dj ON dj.project_id = r.project_id
LEFT JOIN (
  SELECT project_id, count(*) AS num_species FROM taicat_projectspecies GROUP BY project_id
  ) AS sp ON sp.project_id = r.project_id
ORDER BY r.last_upload_time DESC
;'''
        project_list = []
        total_annotations = 0
        total_images = 0
        with connection.cursor() as cursor:
            cursor.execute(sql)
            columns = [col[0] for col in cursor.description]
            #project_list = [dict(zip(columns, row)) for row in cursor.fetchall()]
            for row in cursor.fetchall():
                data = {}
                for i, v in enumerate(row):
                    if isinstance(v, datetime): # cannot marshalize
                        data[f'{columns[i]}__date'] = v.strftime('%Y-%m-%d')
                        data[f'{columns[i]}'] = v.strftime('%Y-%m-%d %H:%M:%S')
                    else:
                        data[columns[i]] = v
                        if columns[i] == 'num_annotations':
                            total_annotations += v
                        elif columns[i] == 'num_images':
                            total_images += v

                project_list.append(data)

        context = {
            'project_list':  project_list,
            'total_images': total_images,
            'total_annotations': total_annotations
        }

        #cache.set('dashboard_context', json.dumps(context), 30) #86400

    return render(request, 'base/admin-dashboard.html', context)


def desktop_login(request):
    code = request.GET.get('t')
    verify_code = code[-4:]
    return render(request, 'base/desktop_login.html', {'verify_code': verify_code})

def desktop_login_verify(request):
    code = request.GET.get('code')
    res = {}
    if user := Contact.objects.filter(desktop_login=code).first():
        user.desktop_login = f'logged-in:{code}'
        user.save()
        res['user_id'] = user.id
        res['name'] = user.name
        res['email'] = user.email

    return JsonResponse(res)


@session_login_required
def desktop(request):
    is_desktop_authorized = False
    user_id = request.session.get('id', None)

    if ProjectMember.objects.filter(member_id=user_id).exists():
        is_desktop_authorized = True
    elif Contact.objects.filter(Q(id=user_id, is_organization_admin=True)|Q(id=user_id, is_system_admin=True)).exists():
        is_desktop_authorized = True
    else:
        is_desktop_authorized = False
    
    announcement = Announcement.objects.order_by('-created').first()

    context = {
        'is_desktop_authorized':is_desktop_authorized,
        'announcement':announcement
    }
    return render(request, 'base/desktop_download.html', context)


def update_is_read(request):
    if request.method == 'GET':
        if contact_id := request.session['id'] :
            UploadNotification.objects.filter(contact_id=contact_id).update(is_read=True)
    return JsonResponse({'data': 'success'}, safe=False) 


def send_upload_notification(upload_history_id, member_list, request):
    site_url = get_request_site_url(request)

    try:
        email_list = []
        email = Contact.objects.filter(id__in=member_list).values('email').exclude(email__isnull=True).exclude(email__exact='')
        for e in email:
            email_list += [e['email']]
        uh = UploadHistory.objects.filter(id=upload_history_id)
        if uh[0].status == 'finished':
            status = '已完成' 
        elif uh[0].status ==  'unfinished':
            status = '未完成' 
        elif uh[0].status ==  'uploading':
            status = '上傳中'

        project_url = '{}/project/info/{}/'.format(site_url, uh[0].deployment_journal.project_id)
        folder_view_url = '{}/project/details/{}/?folder={}'.format(site_url, uh[0].deployment_journal.project_id, uh[0].deployment_journal.folder_name)
        # send email
        html_content = f"""
        您好：
        <br>
        <br>
        以下為系統上傳活動的通知
        <br>
        <br>
        <b>計畫：</b>{uh[0].deployment_journal.project.name}
        <br>
        (<a href="{project_url}" target="_blank">{project_url}</a>)
        <br>
        <br>
        <b>樣區：</b>{uh[0].deployment_journal.studyarea.name}
        <br>
        <br>
        <b>相機位置：</b>{uh[0].deployment_journal.deployment.name}
        <br>
        <br>
        <b>資料夾名稱：</b>{uh[0].deployment_journal.folder_name}
        <br>
        (<a href="{folder_view_url}" target="_blank">{folder_view_url}</a>)
        <br>
        <br>
        <b>上傳狀態：</b>{status}
        <br>"""

        subject = '[臺灣自動相機資訊系統] 上傳通知'
        msg = EmailMessage(subject, html_content, settings.CT_SERVICE_EMAIL, [], email_list)
        msg.content_subtype = "html"  # Main content is now text/html
        # 改成背景執行
        task = threading.Thread(target=send_msg, args=(msg,))
        # task.daemon = True
        task.start()

        return {"status": 'success'}
    except Exception as err_msg:
        return {"status": 'fail', "error_message": err_msg}

@csrf_exempt
def update_upload_history(request):
    # client_status: uploading, finished, upload-start (for annotation processing)
    response = {}
    if request.method == 'POST':
        data = json.loads(request.body)
        client_status = data.get('status', '') #request.POST.get('status')
        deployment_journal_id = data.get('deployment_journal_id') #request.POST.get('deployment_journal_id')
        if not deployment_journal_id:
            # 回傳沒有結果
            response = {'messages': 'failed due to wrong parameters'}
            return JsonResponse(response)

        if client_status in ['uploading', 'image-text']:
            # 把網頁狀態更新成上傳中
            # 若沒有，新增一個uh
            if uh := UploadHistory.objects.filter(deployment_journal_id=deployment_journal_id).first():
                uh.last_updated = timezone.now()
                uh.status = client_status #'uploading'
                uh.save()
            else:
                uh = UploadHistory(
                        deployment_journal_id=deployment_journal_id,
                        status=client_status, #'uploading',
                        last_updated=timezone.now())
                uh.save()
            response = {'messages': 'success'}
        elif client_status == 'finished':
            # 判斷網頁狀態是未完成or已完成, species_error & upload_error
            upload_error = True if Image.objects.filter(deployment_journal_id=deployment_journal_id, has_storage='N').exists() else False
            species_error = True if Image.objects.filter(deployment_journal_id=deployment_journal_id, species__in=[None, '']).exists() else False
            status = 'unfinished' if upload_error or species_error else 'finished'
            if uh := UploadHistory.objects.filter(deployment_journal_id=deployment_journal_id).first():
                uh.last_updated = timezone.now()
                uh.status = status
                uh.upload_error = upload_error
                uh.species_error = species_error
                uh.save()
                upload_history_id = UploadHistory.objects.filter(deployment_journal_id=deployment_journal_id)[0].id
            else:
                uh = UploadHistory(
                        deployment_journal_id=deployment_journal_id,
                        status=status,
                        upload_error = upload_error,
                        species_error = species_error,
                        last_updated=timezone.now())
                uh.save()
                upload_history_id = uh.id

            if status == 'finished':
                DeploymentJournal.objects.filter(pk=deployment_journal_id).update(upload_status='finished')

            if deployment_journal := DeploymentJournal.objects.get(pk=deployment_journal_id):
                project_id = DeploymentJournal.objects.filter(id=deployment_journal_id).values('project_id')[0]['project_id']
                studyarea_id = DeploymentJournal.objects.filter(id=deployment_journal_id).values('studyarea_id')[0]['studyarea_id']
            else:
                response = {'messages': 'failed due to no associated record in DeploymentJournal table'}
                return JsonResponse(response)

            #project_members = get_project_member(project_id) # 所有計劃下的成員
            #studyarea_members = get_studyarea_member(project_id,studyarea_id) # 樣區成員，含總管理人
            #studyarea_none_member = get_none_studyarea_project_member(project_id,['uploader','project_admin'])# 未選擇樣區的資料上傳者/個別計畫管理人
            #studyarea_members.extend(studyarea_none_member)
            #email_list = list(set(studyarea_members)) 

            #final_members = []
            #for m in project_members:
            #    # 每次都建立新的通知
            #    try:
            #        un = UploadNotification(
            #            upload_history_id = upload_history_id,
            #            contact_id = m
            #        )
            #        un.save()
            #        final_members += [m]
            #    except Exception as e:
            #        pass # contact已經不在則移除
            # 每次都寄信
            #res = send_upload_notification(upload_history_id, email_list, request)

            # only send to uploader
            email_list = []
            if deployment_journal.uploader_id:
                email_list.append(int(deployment_journal.uploader_id))
            # append project_admin
            for m in ProjectMember.objects.filter(project_id=project_id).all():
                if m.role == 'project_admin':
                    email_list.append(m.member_id)
            # ignore related organization_admin
            for m in email_list:
                # 每次都建立新的通知
                try:
                    un = UploadNotification(
                        upload_history_id = upload_history_id,
                        contact_id = m
                    )
                    un.save()
                    final_members += [m]
                except Exception as e:
                    pass # contact已經不在則移除

            res = send_upload_notification(upload_history_id, email_list, request)

            if res.get('status') == 'fail':
                response = {'messages': 'failed during sending email'}
                return JsonResponse(response)
            response = {'messages': 'success'}
        else:
            response = {'messages': 'status not allowed'}

    return JsonResponse(response)

# @csrf_exempt
# def check_upload_history(request, deployment_journal_id):
#     response = {}
#     if uh := UploadHistory.objects.filter(deployment_journal_id=deployment_journal_id).first():
#         response.update({
#             'deployment_journal_id': deployment_journal_id,
#             'status': uh.status,
#         })
#         img_ids = {}
#         if uh.status == 'uploading':
#             rows = Image.objects.values('id', 'source_data', 'image_uuid').filter(deployment_journal_id=deployment_journal_id).all()
#             for i in rows:
#                 if id_ := i['source_data'].get('id', ''):
#                     # cloned image has no source_data
#                     img_ids[id_] = [i['id'], i['image_uuid']]
#             response.update({
#                 'saved_image_ids': img_ids,
#             })
#     return JsonResponse(response)

def get_error_file_list(request, deployment_journal_id):
    data = pd.DataFrame(columns=['所屬計畫', '樣區', '相機位置', '資料夾名稱', '檔名', '錯誤類型'])
    query = """
        SELECT p.name, s.name, d.name, i.folder_name, i.filename, i.species, i.has_storage
        FROM taicat_image i
        JOIN base_uploadhistory up ON i.deployment_journal_id = up.deployment_journal_id
        JOIN taicat_project p ON i.project_id = p.id
        JOIN taicat_deployment d ON i.deployment_id = d.id
        JOIN taicat_studyarea s ON i.studyarea_id = s.id
        WHERE i.deployment_journal_id = %s and (has_storage = 'N' OR species IS NULL OR species = '' )"""
    with connection.cursor() as cursor:
        cursor.execute(query, (deployment_journal_id,))
        data = cursor.fetchall()
        data = pd.DataFrame(data, columns=['所屬計畫', '樣區', '相機位置', '資料夾名稱', '檔名', 'species', 'has_storage'])
        data['錯誤類型'] = ''
        data.loc[data['has_storage']=='N', '錯誤類型'] = '影像未成功上傳'
        data.loc[(data['species'].isin([None, ''])) & (data['has_storage']=='Y'), '錯誤類型'] = data.loc[data['species'].isin([None, '']), '錯誤類型'] + '物種未填寫'
        data.loc[(data['species'].isin([None, ''])) & (data['has_storage']=='N'), '錯誤類型'] = data.loc[data['species'].isin([None, '']), '錯誤類型'] + ', 物種未填寫'
        data = data.drop(columns=['species', 'has_storage'])
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename="cameratrap_error.xlsx"'
    data.to_excel(response, index=False)
    return response


def upload_history(request):

    if request.method == 'GET':

        if member_id := request.session.get('id', None):
            is_contractor = ProjectMember.objects.filter(member_id=member_id, role='contractor').exists()
            my_project_list = get_my_project_list(member_id)
            q = request.GET.get('q', '')
            page_number = request.GET.get('page', 1)

            if is_contractor:
                # 先找到樣區 id，再從樣區 id 回推 deploymentJournal id，最後得到對應的 UploadHistory 資訊
                sa = StudyArea.objects.filter(projectmember__project_id__in=my_project_list, projectmember__member_id=member_id, projectmember__role='contractor').values_list('id', flat=True)
                deployment_journal_list = DeploymentJournal.objects.filter(studyarea_id__in=sa)
                print(f'deployment_journal_list:{deployment_journal_list}')
                query = UploadHistory.objects.filter(deployment_journal_id__in=deployment_journal_list).annotate(
                    created_8=ExpressionWrapper(F('created') + timedelta(hours=8),output_field=DateTimeField()),
                    last_updated_8=ExpressionWrapper(F('last_updated') + timedelta(hours=8),output_field=DateTimeField()
                    )).exclude(deployment_journal__deployment__name__isnull=True).values_list('created_8', 'last_updated_8', 'deployment_journal__folder_name', 'deployment_journal__project__name', 'deployment_journal__studyarea__name', 'deployment_journal__deployment__name', 'status', 'deployment_journal__project_id', 'deployment_journal__id', 'species_error', 'upload_error', 'deployment_journal__uploader__name').order_by('-created')
            else:
                query = UploadHistory.objects.filter(deployment_journal__project_id__in=my_project_list).annotate(
                    created_8=ExpressionWrapper(F('created') + timedelta(hours=8),output_field=DateTimeField()),
                    last_updated_8=ExpressionWrapper(F('last_updated') + timedelta(hours=8),output_field=DateTimeField()
                    )).exclude(deployment_journal__deployment__name__isnull=True).values_list('created_8', 'last_updated_8', 'deployment_journal__folder_name', 'deployment_journal__project__name', 'deployment_journal__studyarea__name', 'deployment_journal__deployment__name', 'status', 'deployment_journal__project_id', 'deployment_journal__id', 'species_error', 'upload_error', 'deployment_journal__uploader__name').order_by('-created')

            if q:
                query = query.filter(Q(deployment_journal__project__name__icontains=q) |
                                     Q(deployment_journal__folder_name__icontains=q) |
                                     Q(deployment_journal__studyarea__name__icontains=q) |
                                     Q(deployment_journal__deployment__name__icontains=q) |
                                     Q(deployment_journal__uploader__name__icontains=q))

            paginator = Paginator(query.all(), 20)
            page_obj = paginator.get_page(page_number)
            page_range = paginator.get_elided_page_range(number=page_number)

            return render(request, 'base/upload_history.html', {'page_obj': page_obj, 'page_range': page_range, 'q': q})

    elif request.method == 'POST':
        if x := request.POST.get('q-text'):
            return redirect(f'/upload-history?q={x}')
        else:
            return redirect(f'/upload-history')


def faq(request):
    return render(request, 'base/faq.html')


def contact_us(request):
    return render(request, 'base/contact_us.html')


def feedback_request(request):
    # print(print(request.POST))
    # https://stackoverflow.com/questions/38345977/filefield-force-using-temporaryuploadedfile
    try:
        # print(request.POST)
        # Honeypot: real users can't see this field, bots fill it in
        if request.POST.get('website'):
            return JsonResponse({"status": 'fail'}, safe=False)

        q_detail_type = request.POST.getlist('q-detail-type')
        q_detail_type = ','.join(q_detail_type)
        description = request.POST.get('description')
        email = request.POST.get('email')

        # Reject malformed emails (SQL-injection junk, garbage strings) and
        # RFC 2606 reserved domains (example.{com,org,net}) — only spam bots use them
        try:
            validate_email(email)
        except (ValidationError, TypeError):
            return JsonResponse({"status": 'fail'}, safe=False)
        domain = email.rsplit('@', 1)[-1].lower()
        if domain in ('example.com', 'example.org', 'example.net'):
            return JsonResponse({"status": 'fail'}, safe=False)

        # user = '1'
        user = email.split('@')[0]
        files = request.FILES.getlist('uploaded_file')
        # print(request.FILES.getlist('uploaded_file'))

        # send email
        html_content = f"""
        您好：
        <br>
        <br>
        以下為臺灣自動相機資訊系統收到的問題回饋
        <br>
        <br>
        <b>問題類型：</b>{q_detail_type}
        <br>
        <br>
        <b>問題描述：</b>{description}
        <br>
        <br>
        <b>使用者電子郵件：</b>{email}
        <br>
        """

        subject = '[臺灣自動相機資訊系統] 問題回饋'

        # From must stay CT_SERVICE_EMAIL (verified SES sender), but the
        # feedback has to land in a monitored inbox. camera-trap.tw has no MX
        # record, so mail addressed to noreply@camera-trap.tw is undeliverable —
        # send to CT_BCC_EMAIL_LIST (the staff gmail) instead.
        recipients = [e.strip() for e in settings.CT_BCC_EMAIL_LIST.split(',') if e.strip()]
        msg = EmailMessage(subject, html_content, settings.CT_SERVICE_EMAIL, recipients)
        msg.content_subtype = "html"  # Main content is now text/html
        # # save files to temporary dir
        for f in files:
            # print(f.name)
            fs = FileSystemStorage()
            filename = fs.save(f'email-attachment/{user}_' + f.name, f)
            msg.attach_file(os.path.join('/ct22-volumes/media', filename))

        # 改成背景執行
        task = threading.Thread(target=send_msg, args=(msg,))
        # task.daemon = True
        task.start()

        return JsonResponse({"status": 'success'}, safe=False)
    except:
        return JsonResponse({"status": 'fail'}, safe=False)


def send_msg(msg):
    recipients = (msg.to or []) + (msg.cc or []) + (msg.bcc or [])
    try:
        msg.send()
        logger.info(f'send_msg ok: subject={msg.subject!r} recipients={recipients}')
    except Exception as e:
        logger.error(f'send_msg failed: {e} subject={msg.subject!r} recipients={recipients}', exc_info=True)


def announcement_is_read(request):
    expired_time = 0
    response = {}
    if request.method == 'GET':
        expired_time =  int(Announcement.objects.latest("created").last_updated.strftime('%s')) + 7776000
        response = {'expired_time': expired_time}

    return JsonResponse(response,  safe=False) 


@session_login_required
def announcement(request):
    email_list = []

    # 所有人
    all_ppl = []
    for x in Contact.objects.exclude(email__isnull=True).exclude(email__exact='').values('name','email'):
        all_ppl.append(x['email'])
    # 計畫總管理人 select * from taicat_contact where  is_organization_admin = true;
    organization_admin = []
    for x in Contact.objects.exclude(email__isnull=True).exclude(email__exact='').filter(is_organization_admin=True).values('name','email'):
        organization_admin.append(x['email'])

    # 計畫承辦人 select * from taicat_projectmember where role = 'project_admin';
    project_admin = []
    for x in Contact.objects.exclude(email__isnull=True).exclude(email__exact='').filter(id__in=ProjectMember.objects.filter(role='project_admin').values('member_id')).values('name','email'):
        project_admin.append(x['email'])

    # 資料上傳者 select * from taicat_projectmember where role = 'uploader';
    uploader = []
    for x in Contact.objects.exclude(email__isnull=True).exclude(email__exact='').filter(id__in=ProjectMember.objects.filter(role='uploader').values('member_id')).values('name','email'):
        uploader.append(x['email'])

    # other = []
    # for x in Contact.objects.filter(id=).values('name','email'):
    #     other.append(x['email'])

    email_list = {
        "all_ppl": ','.join(all_ppl),
        "organization_admin": ','.join(organization_admin),
        "project_admin": ','.join(project_admin),
        "uploader": ','.join(uploader),
        # "other" :','.join(other),
    }

    user_id = request.session.get('id')
    is_system_admin = Contact.objects.filter(id=user_id, is_system_admin=True).exists()

    context = {
        'email': email_list,
        'is_system_admin': is_system_admin,
        'test_receiver': settings.ANNOUNCEMENT_TEST_RECEIVER,
    }

    return render(request, 'base/announcement.html',context)


@session_login_required
def announcement_request(request):
    # https://stackoverflow.com/questions/38345977/filefield-force-using-temporaryuploadedfile
    try:
        announcement_title = request.POST.get('announcement-title')
        description = request.POST.get('description').replace('\r\n','<br>')
        email_to = [x.strip() for x in request.POST.get('email').split(',') if x.strip()]

        html_content = f"""
        您好：
        <br>
        <br>
        {description}
        <br>
        <br>
        <br>
        <br>
        <br>
        臺灣自動相機資訊系統 團隊敬上
        """
        subject = f'[臺灣自動相機資訊系統]公告 {announcement_title}'

        # AWS SES: <=50 recipients per call, per-second send rate.
        # Mirror cron_scripts/check_data_gap.py: chunks of 10 with a 2s gap.
        chunk_size = 30

        def send_chunks(recipients):
            for idx in range(0, len(recipients), chunk_size):
                chunk = recipients[idx:idx + chunk_size]
                msg = EmailMessage(subject, html_content, settings.CT_SERVICE_EMAIL, bcc=chunk)
                msg.content_subtype = "html"
                send_msg(msg)

                if idx + chunk_size < len(recipients):
                    time.sleep(2)

        threading.Thread(target=send_chunks, args=(email_to,)).start()

        return JsonResponse({"status": 'success'}, safe=False)
    except Exception as e:
        return JsonResponse({"status": 'fail'}, safe=False)

def policy(request):
    return render(request, 'base/policy.html')


def add_org_admin(request):
    if request.method == 'POST':
        # print('hi')
        for i in request.POST:
            print(i)
        redirect(set_permission)


def login_for_test(request):
    next = request.GET.get('next')
    role = request.GET.get('role')
    info = Contact.objects.filter(name=role).values('name', 'id','orcid').first()
    request.session["is_login"] = True
    request.session["name"] = role
    request.session["orcid"] = info["orcid"]
    request.session["id"] = info['id']
    request.session["first_login"] = False

    return redirect(next)


def set_permission(request):
    is_authorized = False
    user_id = request.session.get('id', None)
    member_list = []
    org_project_list = []
    org_list = []
    project_list = []
    org_admin_list = []
    return_message = ''

    # check permission
    # if Contact.objects.filter(id=user_id).filter(Q(is_organization_admin=True) | Q(is_system_admin=True)):
    if Contact.objects.filter(id=user_id).filter(is_system_admin=True):
        is_authorized = True

        if request.method == 'POST':
            type = request.POST.get('type')
            if type == 'add_admin':
                user_id = request.POST.get('user', None)
                org_id = request.POST.get('organization', None)
                if user_id and org_id:
                    Contact.objects.filter(id=user_id).update(is_organization_admin=True, organization_id=org_id)
                    # messages.success(request, '新增成功')
                    return_message = '新增成功'
            elif type == 'remove_admin':
                user_id = request.POST.get('id', None)
                if user_id:
                    Contact.objects.filter(id=user_id).update(is_organization_admin=False)
                    # messages.success(request, '移除成功')
                    return_message = '移除成功'
            elif type == 'remove_project':
                relation_id = request.POST.get('id', None)
                if relation_id:
                    Organization.projects.through.objects.filter(id=relation_id).delete()
                    # messages.success(request, '移除成功')
                    return_message = '移除成功'
            else:
                project_id = request.POST.get('project', None)
                org_id = request.POST.get('organization', None)
                try:
                    Organization.objects.get(id=org_id).projects.add(Project.objects.get(id=project_id))
                    # messages.success(request, '新增成功')
                    return_message = '新增成功'
                except:
                    # messages.error(request, '新增失敗')
                    return_message = '新增失敗'
        member_list = Contact.objects.all().values('name', 'email', 'id')
        org_list = Organization.objects.all()
        project_list = Project.objects.all().values('name', 'id')

        org_project_set = Organization.projects.through.objects.all()
        for i in org_project_set:
            tmp = {'organization_name': i.organization.name, 'relation_id': i.id,
                   'project_name': i.project.name}
            org_project_list.append(tmp)

        org_admin_list = Contact.objects.filter(is_organization_admin=True).values('organization__name', 'id', 'name', 'email')

    return render(request, 'base/permission.html', {'member_list': member_list, 'org_project_list': org_project_list, 'return_message': return_message,
                    'is_authorized': is_authorized, 'org_list': org_list, 'project_list': project_list, 'org_admin_list': org_admin_list})


def get_auth_callback(request, via):
    #original_page_url = request.GET.get('next')
    authorization_code = request.GET.get('code')
    state = request.GET.get('state') # orcid only support state querystring? (moogoo, 251030)

    # Check if authorization code exists
    if not authorization_code:
        logger.warning('OAuth callback: Missing authorization code')
        messages.error(request, '授權碼遺失，請重新登入')
        return redirect('home')

    data = {'client_id': settings.ORCID_CLIENT_ID,
            'client_secret': settings.ORCID_CLIENT_SECRET,
            'grant_type': 'authorization_code',
            'code': authorization_code}
    token_url = 'https://orcid.org/oauth/token'

    try:
        r = requests.post(token_url, data=data)
        results = r.json()

        # Check for error in response
        if 'error' in results:
            error_description = results.get('error_description', results.get('error'))
            error_type = results.get('error')
            logger.error(f'OAuth callback error: {error_type} - {error_description}')

            if 'reused' in error_description.lower() or error_type == 'invalid_grant':
                messages.error(request, '授權碼已被使用或已過期，請重新登入')
            else:
                messages.error(request, f'登入失敗: {error_description}')
            return redirect('home')

        orcid = results.get('orcid')
        name = results.get('name')

        if not orcid or not name:
            logger.error(f'OAuth callback: Missing user info in response: {results}')
            messages.error(request, '無法取得使用者資訊，請重新登入')
            return redirect('home')

    except requests.RequestException as e:
        logger.error(f'OAuth callback network error: {str(e)}')
        messages.error(request, f'連線失敗: {str(e)}')
        return redirect('home')
    except Exception as e:
        logger.error(f'OAuth callback unexpected error: {str(e)}', exc_info=True)
        messages.error(request, f'登入過程發生錯誤，請重新嘗試')
        return redirect('home')

    # check if orcid exists in db
    if Contact.objects.filter(orcid=orcid).exists():
        # if exists, update login status
        #info = Contact.objects.filter(orcid=orcid).values('name', 'id').first()
        #name = info['name']
        #id = info['id']
        contact = Contact.objects.filter(orcid=orcid).first()
        name = contact.name
        id = contact.id

        #if 'desktop_login' in original_page_url:
        #    url_parts = original_page_url.split('?t=')
        if via == 'desktop_login':
            contact.desktop_login = state #url_parts[1]
            contact.save()

        request.session["first_login"] = False
    else:
        # if not, create one
        new_user = Contact.objects.create(name=name, orcid=orcid)
        id = new_user.id
        # redirect to set email
        request.session["is_login"] = True
        request.session["name"] = name
        request.session["orcid"] = orcid
        request.session["id"] = id
        request.session["first_login"] = True

        #if 'desktop_login' in original_page_url:
        if via == 'desktop_login':
            #url_parts = original_page_url.split('?t=')
            new_user.desktop_login = state #url_parts[1]
            new_user.save()

        return redirect(personal_info)

    request.session["is_login"] = True
    request.session["name"] = name
    request.session["orcid"] = orcid
    request.session["id"] = id


    # Prevent redirect loop - don't redirect back to callback URL
    #if not original_page_url or 'callback/orcid' in original_page_url:
    #    logger.info(f'OAuth callback: Redirecting to home (original_page_url was {original_page_url})')
    #    return redirect('/')
    if state:
        if via == 'web':
            logger.info(f'OAuth callback: Redirecting to home (next_url was {state})')
            return redirect(state)
        elif via == 'desktop_login':
            logger.info(f'OAuth callback success: Redirecting to /desktop_login')
            return redirect(f'/desktop_login?t={state}')


    return redirect('/')


def logout(request):
    request.session["is_login"] = False
    request.session["name"] = None
    request.session["orcid"] = None
    request.session["id"] = None
    return redirect('home')


def personal_info(request):
    # login required
    is_login = request.session.get('is_login', False)
    first_login = request.session.get('first_login', False)
    info = []
    identities = ParameterCode.objects.filter(type='identity').values("name","pmajor","type","parametername")
    if request.method == 'POST':
        first_login = False
        orcid = request.session.get('orcid')
        name = request.POST.get('name')
        email = request.POST.get('email')
        identity = request.POST.get('identity')
        Contact.objects.filter(orcid=orcid).update(name=name, email=email,identity=identity)
        request.session["name"] = name
        
    if is_login:
        info = Contact.objects.filter(
            orcid=request.session["orcid"]).values().first()
        
    return render(request, 'base/personal_info.html', {'info': info, 'first_login': first_login, 'identities': identities})


def home(request):
    announcement = Announcement.objects.order_by('-created').first()
    desktop_version = announcement.version
    context = {'env': settings.ENV,
               'desktop_version': desktop_version}


    return render(request, 'base/home.html',context)


def get_species_data(request):
    now = timezone.now()
    update = False
    last_updated = Species.objects.filter(is_default=True).aggregate(Min('last_updated'))['last_updated__min']
    if last_updated := Species.objects.filter(is_default=True).aggregate(Min('last_updated'))['last_updated__min']:
        if Image.objects.filter(last_updated__gte=last_updated, project__mode='official').exists():
            update = True
    else:
        update = True
    if update:
        Species.objects.filter(is_default=True).update(last_updated=now)
        for i in Species.DEFAULT_LIST:
            c = Image.objects.filter(species=i, project__mode='official').count()
            if Species.objects.filter(is_default=True, name=i).exists():
                # if exist, update
                s = Species.objects.get(is_default=True, name=i)
                s.count = c
                s.last_updated = now
                s.save()
            else:  # else, create new
                if c > 0:
                    new_s = Species(
                        name=i,
                        count=c,
                        last_updated=now,
                        is_default=True
                    )
                    new_s.save()
    # get data
    species_data = []
    with connection.cursor() as cursor:
        query = "SELECT count, name FROM taicat_species WHERE is_default = TRUE ORDER BY count DESC"
        cursor.execute(query)
        species_data = cursor.fetchall()
    response = {'species_data': species_data}
    return HttpResponse(json.dumps(response, cls=DecimalEncoder), content_type='application/json')

# deprecated
def get_geo_data(request):
    with connection.cursor() as cursor:
        query = """SELECT d.longitude, d.latitude, p.name, p.mode FROM taicat_deployment d 
                    JOIN taicat_project p ON p.id = d.project_id 
                    WHERE d.longitude IS NOT NULL and p.mode = 'official';"""
        cursor.execute(query)
        deployment_points = cursor.fetchall()
    response = {'deployment_points': deployment_points}
    return HttpResponse(json.dumps(response, cls=DecimalEncoder), content_type='application/json')


def get_growth_data(request):
    now = timezone.now()
    update = False
    last_updated = HomePageStat.objects.all().aggregate(Min('last_updated'))['last_updated__min']
    if last_updated:
        if Image.objects.filter(created__gte=last_updated, project__mode='official').exists():
            update = True
    else:
        update = True
    if update:
        # HomePageStat.objects.all().update(last_updated=now)
        # ------ update image --------- #
        if last_updated:
            data_growth_image = Image.objects.filter(created__gte=last_updated, project__mode='official').annotate(year=ExtractYear('datetime')).values('year').annotate(num_image=Count('image_uuid', distinct=True)).order_by()
        else: # 完全沒有資料
            data_growth_image = Image.objects.filter(project__mode='official').annotate(year=ExtractYear('datetime')).values('year').annotate(num_image=Count('image_uuid', distinct=True)).order_by()
        data_growth_image = pd.DataFrame(data_growth_image, columns=['year', 'num_image']).sort_values('year')
        year_min, year_max = int(data_growth_image.year.min()), int(data_growth_image.year.max())
        # current_year_max = HomePageStat.objects.aggregate(Max('year')).year__max
        current_year_max = HomePageStat.objects.aggregate(Max('year')).get('year__max')
        if current_year_max:
            if current_year_max > year_max:
                year_max = current_year_max
        year_gap = pd.DataFrame([i for i in range(year_min, year_max+1)], columns=['year'])
        data_growth_image = year_gap.merge(data_growth_image, how='left').fillna(0)
        data_growth_image['cumsum'] = data_growth_image.num_image.cumsum()
        data_growth_image = data_growth_image.drop(columns=['num_image'])
        for i in data_growth_image.index:
            row = data_growth_image.loc[i]
            if HomePageStat.objects.filter(year=row.year).exists():
                h = HomePageStat.objects.get(year=row.year)
                h.count += row['cumsum']
                h.last_updated = now
                h.save()
            else:
                new_h = HomePageStat(
                    count=row['cumsum'],
                    last_updated=now,
                    year=row.year)
                new_h.save()
    data_growth_image = list(HomePageStat.objects.filter(year__gte=2008).order_by('year').values_list('year', 'count'))

    # --------- deployment --------- #
    year_gap = pd.DataFrame([i for i in range(2008, data_growth_image[-1][0]+1)], columns=['year'])
    with connection.cursor() as cursor:
        query = """
                WITH req As(
                    WITH base_request AS (
                            SELECT d.latitude, d.longitude, EXTRACT (year from taicat_project.start_date)::int AS start_year
                            FROM taicat_deployment d
                            JOIN taicat_project ON taicat_project.id = d.project_id 
                            WHERE d.longitude IS NOT NULL
                            GROUP BY start_year, d.latitude, d.longitude)
                            SELECT MIN(start_year) as year , latitude, longitude FROM base_request
                            GROUP BY latitude, longitude)
                    SELECT year, count(*) FROM req GROUP BY year
                """
        cursor.execute(query)
        data_growth_deployment = cursor.fetchall()
        data_growth_deployment = pd.DataFrame(data_growth_deployment, columns=['year', 'num_dep']).sort_values('year')
        data_growth_deployment = year_gap.merge(data_growth_deployment, how='left').fillna(0)
        data_growth_deployment['cumsum'] = data_growth_deployment.num_dep.cumsum()
        data_growth_deployment = data_growth_deployment.drop(columns=['num_dep'])
        data_growth_deployment = list(data_growth_deployment.itertuples(index=False, name=None))

    response = {'data_growth_image': data_growth_image,
                'data_growth_deployment': data_growth_deployment}

    return HttpResponse(json.dumps(response), content_type='application/json')


def stat_county(request):
    if request.method == 'GET':

        county = request.GET.get('county')
        county = county.replace('台','臺')
        response = GeoStat.objects.filter(county = county).values('num_project','num_deployment','identified','num_image','num_working_hour','species', 'studyarea')
        if len(response):
            response = response[0]
            response.update({'species':response.get('species').replace(',','、')})
            sa_points = []
            if response['studyarea']:
                sa_list = response['studyarea'].split(',')
                sa_list = [int(s) for s in sa_list]
                if last_updated := StudyAreaStat.objects.filter(studyarea_id__in=sa_list).aggregate(Min('last_updated'))['last_updated__min']:
                    if Deployment.objects.filter(last_updated__gte=last_updated, study_area_id__in=sa_list).exists():
                        update_studyareastat(sa_list)
                else:
                    update_studyareastat(sa_list)
                with connection.cursor() as cursor:
                    # 首頁地圖只顯示 project_id = 329 的樣區
                    query = """SELECT sas.longitude, sas.latitude, p.name, sa.name, sa.id
                                FROM taicat_studyareastat sas
                                JOIN taicat_studyarea sa ON sas.studyarea_id = sa.id
                                JOIN taicat_project p ON p.id = sa.project_id
                                WHERE sas.studyarea_id = ANY(%s) AND sa.project_id = 329;"""
                    cursor.execute(query,(sa_list,))
                    sa_points = cursor.fetchall()
            response.update({'studyarea': sa_points})
        else:
            response = {'species': '', 'studyarea': [], 'num_project': 0, 'num_deployment': 0,'identified': 0,'num_image': 0,'num_working_hour': 0}
        return HttpResponse(json.dumps(response, cls=DecimalEncoder), content_type='application/json')


def can_view_studyarea_deployment(request, project_id, studyarea_id):
    '''首頁地圖: 是否能看到這個樣區的相機位置

    未登入者一律看不到
    系統管理員 / 該計畫的計畫總管理人: 全部
    有指定樣區的計畫成員: 只有自己的樣區 (即使是同一個計畫)
    沒有指定樣區的計畫成員: 全部
    非計畫成員: 看不到
    '''
    if not request.session.get('is_login', False):
        return False

    user_id = request.session.get('id')
    if Contact.objects.filter(
            Q(id=user_id, is_system_admin=True) |
            Q(id=user_id, is_organization_admin=True, organization__projects=project_id)).exists():
        return True

    members = ProjectMember.objects.filter(project_id=project_id, member_id=user_id)
    if not members.exists():
        return False

    restricted = members.filter(pmstudyarea__isnull=False)
    if not restricted.exists():
        return True

    return restricted.filter(pmstudyarea__id=studyarea_id).exists()


def stat_studyarea(request):
    county_shapes = gpd.read_file(os.path.join(os.path.join(BASE_DIR, "static"),'map/COUNTY_MOI_1090820.shp'))
    if request.method == 'GET':
        county = request.GET.get('county')
        county = county.replace('台','臺')
        print('縣市：', county)
        sa = []
        said = request.GET.get('said')
        # 首頁地圖只顯示 project_id = 329 的相機位置, 且只給有權限的登入者看
        if can_view_studyarea_deployment(request, 329, said):
            query = """SELECT id, longitude, latitude, name, geodetic_datum FROM taicat_deployment WHERE study_area_id = %s AND project_id = 329"""
            with connection.cursor() as cursor:
                cursor.execute(query, (said, ))
                sa = cursor.fetchall()
        name = []
        count = []
        new_sa = []
        for s in sa:
            if s[4] == 'TWD97':
                df = pd.DataFrame({
                            'Lat':[int(s[2])],
                            'Lon':[int(s[1])]})
                geometry = [Point(xy) for xy in zip(df.Lon, df.Lat)]
                gdf = gpd.GeoDataFrame(df, geometry=geometry)
                gdf = gdf.set_crs(epsg=3826, inplace=True)
                gdf = gdf.to_crs(epsg=4326)
                new_sa.append((s[0], gdf.geometry.x[0], gdf.geometry.y[0], s[3]))
            else:
                new_sa.append(s)

        specific_county_geometry = county_shapes.loc[county_shapes['COUNTYNAME'] == county, 'geometry'].iloc[0]
        updated_new_sa = []
        for s in new_sa:
            point = Point(s[1], s[2])
            if specific_county_geometry.contains(point):
                updated_new_sa.append(s)

        for s in updated_new_sa:
            query = """
                    SELECT d.name, COUNT(DISTINCT(i.image_uuid))
                    FROM taicat_image i
                    JOIN taicat_deployment d ON i.deployment_id = d.id
                    WHERE i.deployment_id = %s
                    GROUP BY d.name
                    ORDER BY d.name
                    """
            with connection.cursor() as cursor:
                cursor.execute(query,(s[0],))
                data = cursor.fetchall()
                if len(data):
                    name += [data[0][0]]
                    count += [data[0][1]]
        with connection.cursor() as cursor:
            if last_updated := StudyAreaStat.objects.filter(studyarea_id=said).aggregate(Min('last_updated'))['last_updated__min']:
                if Deployment.objects.filter(last_updated__gte=last_updated, study_area_id=said).exists():
                    update_studyareastat(said)
            else:
                update_studyareastat(said)
            query = f"""SELECT sas.longitude, sas.latitude  
                        FROM taicat_studyareastat sas  
                        WHERE sas.studyarea_id = (%s);"""
            cursor.execute(query, (said,))
            sa_center = cursor.fetchall()

        response = {'name': name, 'count': count, 'deployment_points': updated_new_sa, 'center': sa_center[0]}
        return HttpResponse(json.dumps(response, cls=DecimalEncoder), content_type='application/json')

def api_dashboard(request, chart):

    if chart == 'app_ver':
        # new version
        ver_dict = {}
        dj_list = DeploymentJournal.objects.filter(num_of_images__gt=0).exclude(client_version__isnull=True).exclude(client_version='').values('client_version').annotate(n=Sum('num_of_images')).order_by('client_version')
        for x in dj_list:
            s = x['client_version'].split('(')
            key = f'APP-v{s[0].strip()}'
            ver_dict[key] = ver_dict.get(key, 0) + x['n']

        # old version
        query = "SELECT split_part(memo, '/', 1) as version, COUNT(*) FROM taicat_image WHERE project_id=329 AND memo != '' AND annotation_seq = 0 GROUP BY version ORDER BY version;"
        with connection.cursor() as cursor:
            cursor.execute(query)
            data = cursor.fetchall()

            res = {
                'labels': [x[0] for x in data],
                'data': [x[1] for x in data],
            }
            # no overlap
            for k, v in ver_dict.items():
                res['labels'].append(k)
                res['data'].append(v)

            return JsonResponse(res)

    elif chart == 'top3':
        res = {
            'projects': [],
            'labels': [],
        }
        if ps := Project.objects.filter(id__in=[287, 288, 329]).all():
            for p in ps:
                data = Image.objects.filter(project_id=p.id).annotate(year=ExtractYear('datetime')).values('year').annotate(num_image=Count('id')).order_by('year').all()
                y = {}
                for d in data:
                    if year := d['year']:
                        if int(year) < 2000:
                            res['labels'].append('0000')
                            if '0000' not in y:
                                y['0000'] = d['num_image']
                            else:
                                y['0000'] += d['num_image']
                        else:
                            res['labels'].append(str(year))
                            if str(year) not in y:
                                y[str(year)] = d['num_image']
                            else:
                                y[str(year)] += d['num_image']

                res['projects'].append({
                    'id': p.id,
                    'name': p.name,
                    'years': y,
                    'data': None,
                })
        res['labels'] = sorted(list(set(res['labels'])))
        for proj in res['projects']:
            data = []
            for key in res['labels']:
                x = 0
                if key in proj['years']:
                    x = proj['years'][key]
                data.append(x)
            proj['data'] = data

    elif chart == 'recently':
        a_month_before = (datetime.now()+timedelta(days=-30)+timedelta(hours=8)).strftime('%Y-%m-%d')
        #sql = f"SELECT DATE(i.created), i.deployment_id, d.name, COUNT(*) FROM taicat_image i LEFT JOIN taicat_deployment d ON d.id = i.deployment_id WHERE i.project_id=329 AND i.created >= '{a_month_before}' GROUP BY DATE(i.created), i.deployment_id, d.name ORDER BY DATE(i.created)" # 分相機位置
        labels = []
        date_dict = {}
        date2_dict = {}
        for i in range(1, 32):
            a = datetime.now()+timedelta(days=(i-31))+timedelta(hours=8)
            labels.append([a.strftime('%Y-%m-%d'), a.strftime('%a')])
        #print(labels)


        sql = f"SELECT DATE(created), COUNT(*) FROM taicat_image WHERE project_id=329 AND created >= '{a_month_before}' GROUP BY DATE(created) ORDER BY DATE(created)"
        with connection.cursor() as cursor:
            cursor.execute(sql)
            data = cursor.fetchall()
            for i in data:
                date_dict[i[0].strftime('%Y-%m-%d')] = i[1]

        sql = f"SELECT DATE(created), COUNT(*) FROM taicat_image WHERE project_id=329 AND created >= '{a_month_before}' AND has_storage = 'Y' GROUP BY DATE(created) ORDER BY DATE(created)"
        with connection.cursor() as cursor:
            cursor.execute(sql)
            data = cursor.fetchall()
            for i in data:
                date2_dict[i[0].strftime('%Y-%m-%d')] = i[1]

            data = []
            data_has_storage = []
            for d in labels:
                if x:= date_dict.get(d[0]):
                    data.append(x)
                else:
                    data.append(0)
                if x:= date2_dict.get(d[0]):
                    data_has_storage.append(x)
                else:
                    data_has_storage.append(0)

            res = {'data': data, 'labels': [f'{x[0]} {x[1]}' for x in labels], 'data_has_storage': data_has_storage}

    return JsonResponse(res)

def page_404(request, exception=None):
    return render(request, 'base/404.html', status=404)

@session_login_required
def download_desktop_file(request):
    file_path = os.path.join(settings.MEDIA_ROOT, 'desktop', 'desktop.zip')
    is_desktop_authorized = False
    user_id = request.session.get('id', None)

    if ProjectMember.objects.filter(member_id=user_id).exists():
        is_desktop_authorized = True
    elif Contact.objects.filter(Q(id=user_id, is_organization_admin=True)|Q(id=user_id, is_system_admin=True)).exists():
        is_desktop_authorized = True
    else:
        is_desktop_authorized = False
    
    if is_desktop_authorized:
        try:
            return FileResponse(open(file_path, 'rb'))
        except FileNotFoundError:
            return page_404(request)
    else:
        return page_404(request)
