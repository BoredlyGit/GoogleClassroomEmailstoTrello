import imaplib2
import requests
import json
import email, email.policy
import datetime
import random
import time
import base64
import binascii
import logging
import sys


logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
formatter = logging.Formatter("[{asctime} | {levelname}]: {message}", style="{", datefmt="%m-%d-%Y %I:%M:%S %p")

file_handler = logging.FileHandler("ClassroomToTrello.log", "a")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)


# TODO: handle questions
class JsonFileSettingsDict(dict):
    class MissingKeyError(Exception):
        """
        Used to ensure that certain fields are present in here. Does NOT verify the value of these keys.
        """
        pass

    def __init__(self, file_path: str, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.file_path = file_path
        with open(file_path, "r") as f:
            self.update(json.load(f))

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        with open(self.file_path, "w") as f:
            json.dump(self, f, indent=4)

    def update(self, *args, **kwargs) -> None:
        super().update(*args, **kwargs)
        with open(self.file_path, "w") as f:
            json.dump(self, f, indent=4)

    def confirm_keys(self, keys):
        for keys in keys:
            if keys not in self.keys():
                raise self.MissingKeyError(keys)


class TrelloCard:
    def __init__(self, title, subject, classroom_type, due_date=None, description=""):
        self.state = "DRAFT"
        self.title = title
        self.subject_label = subject
        self.due_date = due_date
        self.description = description
        self.classroom_type = classroom_type

    @classmethod
    def from_email(cls, email_message: email.message.Message):  # Supports pre-redesign emails, just in case.
        assert "New assignment" in email_message["subject"] or "New material" in email_message["subject"]
        assert email_message["from"].endswith("classroom.google.com>")
        assert email_message.is_multipart()  # payload 1 is raw text, 2 is html

        logger.info(f'Generating card for: Subject: {email_message["subject"]}')
        logger.debug(f"!!DEBUG{'='*100}")

        text = [msg.get_payload() for msg in email_message.get_payload() if msg.get_content_disposition() is None][0]
        try:  # Occasionally the the plaintext is b64 encoded for some reason?? idk
            text = base64.b64decode(text).decode()
        except (UnicodeDecodeError, binascii.Error):
            pass
        text = text.replace("\r", "")
        logger.debug(f"original text: \n {text}")

        classroom_type = email_message["subject"].split(" ")[1].lower().replace(":", "")

        logger.debug(f"classroom_type: {classroom_type}")
        og_title = email_message["subject"].split('"', 1)[1][:-1].replace("\r", "").replace("\n ", "\n")
        title = " ".join(og_title.split()).replace("\n", "")
        title = title[:-1] if title.endswith(" ") else title
        logger.debug(f"title: {[title]}, {title.endswith(' ')}\nog_title: {[og_title]}")

        subject_label = text.split("\n<https://classroom.google.com/c/")[0].split(f" posted a new {classroom_type} in ")[1]
        logger.debug(f"subject_label: {subject_label}")

        try:
            url = "https://classroom.google.com/c/{}".format(text.split("\nOPEN  \n<https://classroom.google.com/c/")[1].split("/details>\n")[0])
        except IndexError:
            url = "https://classroom.google.com/c/{}".format(text.split("\nOpen  \n<https://classroom.google.com/c/")[1].split("/details>\n")[0])
        logger.debug(f"url: {url}")
        logger.debug(f"text.split(title) debug: {text.split(title), len(text.split(title)), title in text, type(text)}")
        try:
            description = text.split(og_title)[1].split("\nOPEN  \n<https://classroom.google.com/c/")[0].split("\nOpen  \n<https://classroom.google.com/c/")[0]
        except IndexError:
            description = text.split(title)[1].split("\nOPEN  \n<https://classroom.google.com/c/")[0].split("\nOpen  \n<https://classroom.google.com/c/")[0]

        description = f"{url}\n\n{description}"
        logger.debug(f"description: {description}")

        date = None
        if classroom_type == "assignment":
            try:
                date = text.split(">.\n\n")[1].split(f"\n{title}")[0].replace("Due: ", "").replace("New assignment Due ", "")
                date = datetime.datetime.strptime(date, "%b %d")
                # If the date is in or past Jan, but before Sep, year is increased by 1
                date = date.replace(year=datetime.datetime.now().year + 1 if 9 > date.month >= 1 else datetime.datetime.now().year)
            except ValueError:
                date = None
        logger.debug(f"date: {date}")
        logger.debug(f"!!END_DEBUG{'='*100}\n\n")
        return cls(title, subject_label, classroom_type, date, description)


class Main:
    def __init__(self):
        try:
            self.settings = JsonFileSettingsDict("config.json")
            self.settings.confirm_keys(["USERNAME", "TRELLO_KEY", "TRELLO_TOKEN"])
        except (FileNotFoundError, JsonFileSettingsDict.MissingKeyError):
            self.settings = self.initialize_settings()

        self.USERNAME = ""
        self.PWD = ""
        self.latest_checked_email_num = 1

        self.TRELLO_KEY = ""
        self.TRELLO_TOKEN = ""

        self.BOARD_ID = ""
        self.ASSIGNMENTS_LIST_ID = ""
        self.MATERIALS_LIST_ID = ""

        self.settings = JsonFileSettingsDict("config.json")
        for attr, value in self.settings.items():
            setattr(self, attr, value)

        self.auth_query_params = f"?key={self.TRELLO_KEY}&token={self.TRELLO_TOKEN}"
        self.labels = {}

        self.imap_conn = imaplib2.IMAP4_SSL("imap.gmail.com", 993)
        self.imap_conn.login(self.USERNAME, self.PWD)

    def fetch_labels(self):
        for label in requests.get(f"https://trello.com/1/boards/{self.BOARD_ID}/labels{self.auth_query_params}").json():
            self.labels[label["name"]] = label["id"]

    @staticmethod
    def initialize_settings():
        def input_one_of_iterable(iterable, name_field: str, input_message: str):
            while True:
                try:
                    i = 1
                    for item in iterable:
                        print(f"{i}) {item[name_field]}")
                        i += 1
                    return iterable[int(input(f"{input_message} (input the number next to it): ")) - 1]

                except (ValueError, IndexError):
                    print("Invalid choice. Please input the number next to your selection.")

        print("No settings found or file invalid, initializing...\nYou can change these settings at any time in the"
              " config.json file, or restart this process by deleting it.")
        settings = {
            "latest_checked_email_num": 1,
            "USERNAME": input("Email address: "),
            "PWD": input("Email password: "),
            "TRELLO_KEY": input("Go to https://trello.com/app-key and paste the key here: "),

        }
        settings["TRELLO_TOKEN"] = input(f"Go to https://trello.com/1/authorize?name=ClassroomToTrello&scope=read,"
                                         f"write&response_type=token&key={settings['TRELLO_KEY']}, "
                                         f"allow access, and paste the token here: ")

        while True:
            try:
                imap_conn = imaplib2.IMAP4_SSL("imap.gmail.com", 993)
                imap_conn.login(settings["USERNAME"], settings["PWD"])
                break
            except imaplib2.IMAP4.error:
                print("Invalid email credentials. Please try again.")
                settings["USERNAME"] = input("Email address: ")
                settings["PWD"] = input("Email password: ")

        while True:  # Validate trello key and token
            trello_auth_conf = requests.get(f"https://api.trello.com/1/members/me/?key={settings['TRELLO_KEY']}&token="
                                            f"{settings['TRELLO_TOKEN']}")
            trello_auth_conf_text = trello_auth_conf.text

            if "invalid key" in trello_auth_conf_text:
                settings["TRELLO_KEY"] = input("Go to https://trello.com/app-key and paste the key here. "
                                               "The previous key was incorrect or invalid: ")
            elif "invalid token" in trello_auth_conf_text:
                settings["TRELLO_TOKEN"] = input(f"Go to https://trello.com/1/authorize?name=ClassroomToTrello&scope=read,"
                                                 f"write&response_type=token&key={settings['TRELLO_KEY']}, "
                                                 f"allow access, and paste the token here. The previous token was"
                                                 f" incorrect or invalid: ")
            else:
                break

        auth_query_params = f"?key={settings['TRELLO_KEY']}&token={settings['TRELLO_TOKEN']}"
        trello_auth_conf = trello_auth_conf.json()

        settings["BOARD_ID"] = input_one_of_iterable(
            [requests.get(f"https://trello.com/1/boards/{board_id}{auth_query_params}").json()
             for board_id in trello_auth_conf["idBoards"]],
            "name", "Select which board you would like to use")["id"]

        settings["ASSIGNMENTS_LIST_ID"] = input_one_of_iterable(
            requests.get(f"https://trello.com/1/boards/{settings['BOARD_ID']}/lists{auth_query_params}").json(),
            "name", "Select which list you would like to use for assignments")["id"]

        settings["MATERIALS_LIST_ID"] = input_one_of_iterable(
            requests.get(f"https://trello.com/1/boards/{settings['BOARD_ID']}/lists{auth_query_params}").json(),
            "name", "Select which list you would like to use for materials")["id"]

        with open("config.json", "w") as f:
            json.dump(settings, f)

        print("Successfully initialized settings!")
        return JsonFileSettingsDict("config.json")

    def create_trello_post_dict(self, **kwargs):
        a = {"key": self.TRELLO_KEY, "token": self.TRELLO_TOKEN,}
        a.update(kwargs)
        return a

    def create_card(self, card: TrelloCard):
        label_id = self.labels.get(card.subject_label)
        if label_id is None:
            logger.info(f"TRELLO: Creating missing label: {card.subject_label}")
            label_id = requests.post(f"https://trello.com/1/boards/{self.BOARD_ID}/labels",
                                     self.create_trello_post_dict(**{
                                         "id": self.BOARD_ID,
                                         "name": card.subject_label,
                                         "color": random.choice(("green", "yellow", "orange", "red", "purple", "blue",
                                                                 "sky", "lime", "pink", "black")),
                                     })).json()["id"]

        assert requests.post("https://trello.com/1/card",
                             self.create_trello_post_dict(**{
                                 "idList": getattr(self, f"{card.classroom_type.upper()}S_LIST_ID"),
                                 "name": card.title,
                                 "desc": card.description,
                                 "due": card.due_date.strftime("%Y-%m-%d") if card.due_date is not None else None,
                                 "idLabels": label_id
                                 })).status_code < 400

        card.state = "UPLOADED"
        return card

    def main(self):
        logger.info(f"{'='*100}\nStarting!")
        self.fetch_labels()
        latest_message_num = int(self.imap_conn.select('INBOX')[1][0]) + 1
        logger.info(f"Latest message in inbox: {latest_message_num}\n")

        for i in range(self.latest_checked_email_num, latest_message_num):
            self.settings["latest_checked_email_num"] = i
            msg = email.message_from_bytes(
                self.imap_conn.fetch(str(i).encode(), "(RFC822 BODY.PEEK[])", policy=email.policy.default)[1][0][1])
            try:
                self.create_card(TrelloCard.from_email(msg))
            except AssertionError:
                logger.info(f'''IGNORED: Subject: {[msg['subject']]} | Received {msg["Date"]}''')
            time.sleep(0.5)  # Rate limits

        while True:
            self.imap_conn.idle()
            latest_message_num += 1
            msg = email.message_from_bytes(self.imap_conn.fetch(str(latest_message_num).encode(), "(RFC822 BODY.PEEK[])", policy=email.policy.default)[1][0][1])
            try:
                self.create_card(TrelloCard.from_email(msg))
            except AssertionError:
                logger.info(f'''IGNORED: Subject: {[msg['subject']]} | Received: {msg["Date"]}''')
            time.sleep(0.5)  # Rate limits

    def run_forever(self):
        while True:
            try:
                self.main()
            except Exception as e:  # run at all costs, exceptions will be added later (ie authentication)
                logger.error("\n\nERROR in run_forever():", exc_info=sys.exc_info())


Main().main()
# a = tk.Tk()
# a.after(1, a.destroy)
# tk.mainloop()
print("wasd")

