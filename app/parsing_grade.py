# [{"status":0,"input_file":"1.in","output":"","time":0.012752833,"memory":12320},{"status":0,"input_file":"2.in","output":"","time":0.014071334,"memory":12320},{"status":0,"input_file":"3.in","output":"","time":0.014515814,"memory":12320},{"status":0,"input_file":"4.in","output":"","time":0.133776501,"memory":14132},{"status":4,"input_file":"5.in","output":"","time":1.589012213,"memory":14144},{"status":4,"input_file":"6.in","output":"","time":3.000535844,"memory":13632}]

import json
import pandas as pd

STATUS_AC = 0
STATUS_WA = 1
STATUS_WF = 11
STATUS_CPY = 12
STATUS_WFN = 13
STATUS_TOOL_CHEAT = 14  # Gian lận: sử dụng tool với đường dẫn ghi đè


def is_json(myjson):
  try:
    json.loads(myjson)
  except ValueError as e:
    return False
  return True


def grade_interpreter_from_ws(data, rubrik = []):
    output_json = '{}'
    # string to JSON

    if type(data) == str:
        if not is_json(data):
            return "{}"
        else:
            data = json.loads(data)

    # Iterating through the json
    # list
    # score_list = []
    # print('data nhận được hiện là: \n')
    # print(data)

    for i in data: # chi co 1 phan tu duy nhat tu ws tra lai
        # print(i)
        sv = data[i]
        temp = i.split('.')
        score = dict()
        score['Student email '] = i.replace("."+temp[-1], "")
        score['Lab name'] = temp[-1]
        score['Score'] = "%.1f" % 0
        score['Note'] = ''
        score['Status'] = ''
        last_status = STATUS_AC # dùng để điều chỉnh mã lỗi
        status_list = []
        # Kiểm tra copy — vẫn override từng item như cũ
        if (len(sv['actualwatermark']) == 0) or (sv['actualwatermark'] != sv['expectedwatermark']):
            score['Note'] = 'copy from other!'
            last_status = STATUS_CPY # mã lỗi cheating IR: invalid return
        # else:
        # Cham diem
        sv_grades = sv['grades']
        # Lay set cac goal bi gian lan tool (per-goal, khong override tat ca)
        cheated_goals = set(sv.get('cheated_goals', []))

        sumg = 0
        sum_rubrik = sum(rubrik)
        count = 0
        if len(sv_grades) > 0:
            for item in sv_grades:
                # 24/3/2023: Bỏ tất cả các đầu điểm bắt đầu với "_" do labtainer không chấm
                if item[0] != '_':
                    status = dict()
                    status['output'] = ''
                    status['input_file'] = item
                    status['status'] = STATUS_WA  # WA trả lời sai
                    if type(sv_grades[item]) == bool:
                        if sv_grades[item] == True:
                            status['status'] = STATUS_AC # AC trả lời đúng
                            if not rubrik:
                                sumg += 1
                            else:
                                sumg += rubrik[count]
                    else:
                        if type(sv_grades[item]) == int:
                            if sv_grades[item] > 0:
                                status['status'] = STATUS_AC # AC
                                if not rubrik:
                                    sumg += 1
                                else:
                                    sumg += rubrik[count]
                    if last_status == STATUS_CPY: # copy bài
                        print('OK')
                        status['status'] = STATUS_CPY
                        print('OK')
                        sumg = 0
                    elif item in cheated_goals:
                        # Goal nay dung tool gian lan → ghi de, khong tinh diem
                        status['status'] = STATUS_TOOL_CHEAT
                        if status['status'] == STATUS_AC or sv_grades[item]:
                            # da tru di phan diem vua cong o tren
                            if not rubrik:
                                sumg = max(0, sumg - 1)
                            else:
                                sumg = max(0, sumg - rubrik[count])

                    status_list.append(status)
                    count += 1

        # Neu co bat ky goal bi gian lan, ghi note
        if cheated_goals and last_status != STATUS_CPY:
            score['Note'] = 'tool cheating detected!'

        if len(sv_grades) > 0:
            if not rubrik:
                sv_score = "%.1f" % round(10 * sumg / len(sv_grades), 1)
            else:
                sv_score = "%.1f" % round(10 * sumg / sum_rubrik, 1)
            score['Score'] = sv_score
            score['Status'] = status_list
        else: # Nếu không có item nào cả thì trả về mã lỗi
            score['Score'] = 0
            if sv.get('tool_cheat', False):
                cheat_detail = sv.get('tool_cheat_detail', '')
                score['Status'] = [{"output": cheat_detail, "input_file": "tool_cheating_no_goals", "status": STATUS_TOOL_CHEAT}]
            elif last_status == STATUS_CPY:
                score['Status'] = [{"output": "", "input_file": "copy from " + str(sv['firstlevelzip']).replace('{}','') + str(sv['secondlevelzip']).replace('{}',''), "status": STATUS_CPY}] # Ma loi 12: copy tu nguoi khac
            else:
                score['Status'] = [{"output": "", "input_file": "", "status": STATUS_AC}]
        # score_list.append(score)
        break

    return score

# def json2score(file_name_json, rubrik = [10,15,20,10]):
#     output_json = '{}'
#     # Opening JSON file
#     try:
#         with open(file_name_json) as f:
#             # returns JSON object as
#             # a dictionary
#             data = json.load(f)
#     except FileNotFoundError:
#         msg = "The file " + file_name_json + " does not exist."
#         print(msg)
#         return msg
#
#     # Iterating through the json
#     # list
#     score_list = []
#     for i in data:
#         # print(i)
#         sv = data[i]
#         temp = i.split('.')
#         score = dict()
#         score['Student email '] = i.replace("."+temp[-1], "")
#         score['Lab name'] = temp[-1]
#         score['Score'] = "%.1f" % 0
#         score['Note'] = ''
#         if sv['actualwatermark'] != sv['expectedwatermark']:
#             score['Note'] = 'copy from other!'
#         else:
#             # Cham diem
#             sv_grades = sv['grades']
#             sum = 0
#             for item in sv_grades:
#                 if type(sv_grades[item]) == bool:
#                     if sv_grades[item] == True:
#                         sum+=1
#                 else:
#                     if type(sv_grades[item]) == int:
#                         if sv_grades[item] > 0:
#                             sum+=1
#             sv_score = "%.1f" % round(10*sum/len(sv_grades),1)
#             score['Score'] = sv_score
#         score_list.append(score)
#
#     # Closing file
#     f.close()
#
#     output_json = json.dumps(score)
#     return output_json

def parsing_gradedata(json_data):
    if type(json_data) == str:
        if not is_json(json_data):
            return '[{"output": "", "input_file": "exception_occured", "status": ' + str(STATUS_WF) + '}]' # Ma loi 2 : nop nham file/ sai dinh dang file
        else:
            json_data = json.loads(json_data)

    ret = grade_interpreter_from_ws(json_data)  # ret: trả về kết quả gồm cả thông tin của
    # print(ret)
    output_json = json.dumps(ret['Status'])
    return output_json

#
# file_name_json = 'gradetest.json'
# try:
#     with open(file_name_json) as f:
#         # returns JSON object as
#         # a dictionary
#         grade_data = json.load(f)
#         grade_data = json.dumps(grade_data)
# except FileNotFoundError:
#     msg = "The file " + file_name_json + " does not exist."
#     print(msg)
#
#
# ret = grade_interpreter_from_ws(grade_data) # ret: trả về kết quả gồm cả thông tin của
# print(ret)
# Kết quả: {'Student email ': 'tuanta.b18at218_at_stu.ptit.edu.vn', 'Lab name': 'bufoverflow', 'Score': '6.7', 'Note': 'copy from other!', 'Status': [{'output': '', 'input_file': 'gain_root_priv', 'status': 0}, {'output': '', 'input_file': '_aslron', 'status': 1}, {'output': '', 'input_file': 'while_run', 'status': 0}, {'output': '', 'input_file': 'stack_protect', 'status': 0}, {'output': '', 'input_file': '_whiledump', 'status': 0}, {'output': '', 'input_file': '_whileroot', 'status': 1}]}

# output_json = parsing_grade(grade_data)
# print(output_json) # output_json trả về kết quả để hiển thị lên Web
# Kết quả ra là
#[{"output": "", "input_file": "gain_root_priv", "status": 0}, {"output": "", "input_file": "_aslron", "status": 1}, {"output": "", "input_file": "while_run", "status": 0}, {"output": "", "input_file": "stack_protect", "status": 0}, {"output": "", "input_file": "_whiledump", "status": 0}, {"output": "", "input_file": "_whileroot", "status": 1}]


