import json
from flask import Flask, request, jsonify
import openai
from langchain.prompts import (
    ChatPromptTemplate,
    MessagesPlaceholder,
    SystemMessagePromptTemplate,
    HumanMessagePromptTemplate,
)
from langchain.chains import LLMChain
from langchain.chat_models import ChatOpenAI
from langchain.memory.chat_memory import BaseChatMemory
from langchain.schema.language_model import BaseLanguageModel
from langchain.schema.messages import BaseMessage, get_buffer_string
from langchain.memory.chat_message_histories import RedisChatMessageHistory
from langchain.memory.chat_message_histories import FileChatMessageHistory
from flask import Flask, request
from dotenv import load_dotenv
from langchain.callbacks.base import BaseCallbackHandler
from langchain.schema import LLMResult
from typing import Any, Union, Dict, List
import secrets
import threading
import re
import os
import redis
import logging
from logging.handlers import TimedRotatingFileHandler


app = Flask(__name__)


MIN_LENGTH = 20

END_MARK = "<END>"

load_dotenv()

logger = logging.getLogger()
logger.setLevel(logging.INFO)
handler = TimedRotatingFileHandler(
    'app.log', when='midnight', interval=1, backupCount=7)
handler.suffix = '%Y-%m-%d'
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)


API_KEY = os.getenv("API_KEY")
openai.proxy = os.getenv("PROXY_URL")
#
openai.api_base = os.getenv("OPEN_AI_BASE")

MODEL = os.environ.get("MODEL", "gpt-3.5-turbo")
MAX_TOKEN_LIMIT = int(os.environ.get('MAX_TOKEN_LIMIT', 2000))

USE_REDIS_CACHE = os.environ.get('REDIS_HOST', None) is not None
REDIS_HOST = os.environ.get('REDIS_HOST', 'redis')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))
REDIS_PASSWORD = os.environ.get('REDIS_PASSWORD', None)
REDIS_DB = int(os.environ.get('REDIS_DB', 0))
REDIS_URL = f'redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}'
if REDIS_PASSWORD is not None:
    REDIS_URL = f'redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}'

CACHE_PATH = os.environ.get('CACHE_PATH', "./chat_history")
SYSTEM_TEMPLATE = os.environ.get(
    'SYSTEM_TEMPLATE', "You are a nice chatbot having a conversation with a person.")

os.path.exists(CACHE_PATH) or os.makedirs(CACHE_PATH)

redis_client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    password=REDIS_PASSWORD,
    db=REDIS_DB)


def read_aws_text(session_id, question_id):
    cache_key = get_cache_key(session_id, question_id)
    if USE_REDIS_CACHE:
        msg = redis_client.get(cache_key)
        if msg:
            return msg.decode("utf-8")
        return None
    else:
        if not os.path.exists(f"{cache_key}.txt"):
            return ""
        with open(f"{cache_key}.txt", "r") as f:
            return f.read()


def append_to_aws_text(cache_key, message):
    # logger.info(f"{cache_key} append_to_aws_text: {message}")
    if USE_REDIS_CACHE:
        append_text_to_redis(cache_key, message)
    else:
        with open(f"{cache_key}.txt", "a") as f:
            f.write(message)


def remove_aws_text(cache_key):
    if USE_REDIS_CACHE:
        redis_client.delete(cache_key)
    else:
        if os.path.exists(f"{cache_key}.txt"):
            os.remove(f"{cache_key}.txt")


def cut_sent(para):
    para = re.sub('([。！？\?])([^”’])', r"\1\n\2", para)
    para = re.sub('(\.{6})([^”’])', r"\1\n\2", para)
    para = re.sub('(\…{2})([^”’])', r"\1\n\2", para)
    para = re.sub('([。！？\?][”’])([^，。！？\?])', r'\1\n\2', para)
    para = para.rstrip()
    return para.split("\n")


def is_sentence_ended(text):
    punctuation_marks = [".", "?", "!", "...",
                         ";", ":", "。", "？", "！", "……", "；", "："]
    last_char = text[-1]
    return last_char in punctuation_marks


def is_token_auth(token):
    if not token and not API_KEY:
        return True
    if not token or not API_KEY:
        return False
    keys = API_KEY.split(",")
    return token in keys


def split_text(text):
    if not text:
        return "", None

    text = text.lstrip()
    sentences = cut_sent(text)

    if len(sentences) == 1:
        if is_sentence_ended(text):
            return text, ""
        return "", None

    last_sentence = sentences[-1]
    if not is_sentence_ended(last_sentence):
        sentences.pop()

    total_length = 0
    for i, sentence in enumerate(sentences):
        total_length += len(sentence)
        if total_length >= MIN_LENGTH:
            break

    if total_length >= MIN_LENGTH:
        message = text[:total_length]
        remaining_text = text[total_length:]
        return message, remaining_text

    return "", None


def append_text_to_redis(redis_key, text):

    content = redis_client.get(redis_key)
    if content is not None:
        content = content.decode("utf-8") + text
    else:
        content = text
    redis_client.set(redis_key, content)


def get_cache_key(session_id, question_id):
    return f"{CACHE_PATH}/{session_id}_{question_id}"


class CustomTokenMemory(BaseChatMemory):
    new_buffer: List = []
    human_prefix: str = "Human"
    ai_prefix: str = "AI"
    llm: BaseLanguageModel
    memory_key: str = "history"
    max_token_limit: int = 2000

    @property
    def buffer(self) -> List[BaseMessage]:
        """String buffer of memory."""
        if not self. new_buffer:
            self. prune_memory()
        return self. new_buffer

    @property
    def memory_variables(self) -> List[str]:
        """Will always return list of memory variables.

        :meta private:
        """
        return [self. memory_key]

    def load_memory_variables(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Return history buffer."""
        buffer: Any = self.buffer
        if self. return_messages:
            final_buffer: Any = buffer
        else:
            final_buffer = get_buffer_string(
                buffer,
                human_prefix=self.human_prefix,
                ai_prefix=self.ai_prefix,
            )
        return {self. memory_key: final_buffer}

    def save_context(self, inputs: Dict[str, Any], outputs: Dict[str, str]) -> None:
        """Save context from this conversation to buffer. Pruned."""
        super().save_context(inputs, outputs)
        self. prune_memory()

    def prune_memory(self):
        # Prune buffer if it exceeds max token limit
        buffer = self.chat_memory.messages
        curr_buffer_length = self.llm.get_num_tokens_from_messages(buffer)
        if curr_buffer_length > self.max_token_limit:
            while curr_buffer_length > self.max_token_limit:
                buffer. pop(0)
                curr_buffer_length = self.llm.get_num_tokens_from_messages(
                    buffer)
        self.new_buffer = buffer


class StreamingGradioCallbackHandler(BaseCallbackHandler):

    def __init__(self, redis_key):
        self.redis_key = redis_key

    def on_llm_start(
        self, serialized: Dict[str, Any], prompts: List[str], **kwargs: Any
    ) -> None:
        pass

    def on_retry(self):
        logger.info("ignore_retry")
        return True

    def ignore_retry(self):
        logger.info("ignore_retry")
        pass

    def on_llm_new_token(self, token: str, **kwargs: Any) -> None:
        append_to_aws_text(self.redis_key, token)

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        append_to_aws_text(self.redis_key, END_MARK)

    def on_llm_error(
        self, error: Union[Exception, KeyboardInterrupt], **kwargs: Any
    ) -> None:
        append_to_aws_text(self.redis_key, END_MARK)


def ask_question(session_id, question_id, question):
    redis_key = get_cache_key(session_id, question_id)
    callbackhandler = StreamingGradioCallbackHandler(redis_key)
    llm = ChatOpenAI(streaming=True, callbacks=[callbackhandler])
    if USE_REDIS_CACHE:
        message_history = RedisChatMessageHistory(
            url=REDIS_URL, ttl=600, session_id=session_id)
    else:
        message_history = FileChatMessageHistory(
            file_path=f"chat_history/{session_id}.json")
    memory = CustomTokenMemory(
        llm=llm,
        max_token_limit=MAX_TOKEN_LIMIT,
        memory_key="chat_history",
        return_messages=True,
        chat_memory=message_history)
    prompt = ChatPromptTemplate(
        messages=[
            SystemMessagePromptTemplate.from_template(
                SYSTEM_TEMPLATE
            ),
            MessagesPlaceholder(variable_name="chat_history"),
            HumanMessagePromptTemplate.from_template("{question}")
        ]
    )
    chain = LLMChain(
        llm=llm,
        prompt=prompt,
        verbose=True,
        memory=memory,
    )
    resp = chain({"question": question})
    answer = resp["text"]
    logger.info(f"{session_id} answer: {answer}")


@app.route('/api/ask', methods=['POST'])
def ask():
    token = request.headers.get('token')
    if not is_token_auth(token=token):
        return jsonify({'status': 'fail'})
    data = request.get_json()
    question = data.get('question')
    session_id = data.get('session_id')
    question_id = secrets.token_hex(8)
    if session_id is None or len(session_id) == 0:
        session_id = secrets.token_hex(16)
    logger.info(f"{session_id} question: {question}")
    thread = threading.Thread(target=ask_question, args=(
        session_id, question_id, question,))
    thread.start()
    return jsonify({'status': 'ok', 'session_id': session_id, 'question_id': question_id})


@app.route('/api/answer', methods=['POST'])
def answer():
    token = request.headers.get('token')
    if not is_token_auth(token=token):
        return jsonify({'status': 'fail'})
    data = request.get_json()
    session_id = data.get('session_id')
    question_id = data.get('question_id')
    if session_id is None or question_id is None:
        return jsonify({'status': 'fail'})
    msg = read_aws_text(session_id, question_id)
    if msg is not None:
        if msg.endswith(END_MARK):
            msg = msg.rstrip(END_MARK)
            resp = jsonify({'status': 'end', 'msg': msg})
            remove_aws_text(get_cache_key(session_id, question_id))
        else:
            value1, _ = split_text(msg)
            resp = jsonify({'status': 'running', 'msg': value1})
    else:
        resp = jsonify({'status': 'end'})
    return resp


@app.route('/api/chat_history', methods=['GET'])
def list_chat_history():
    chat_history_list = read_chat_history_list()
    return jsonify({'status': 'ok', "chat_history_list": chat_history_list})


def read_chat_history_list():
    chat_history_list = []
    for file_path in os.listdir(CACHE_PATH):
        if file_path.endswith(".json"):
            chat_history_list.append(file_path)
    return chat_history_list


@app.route('/api/chat_history/<path:session_id>/text', methods=['GET'])
def read_session_text(session_id):
    file_path = f"{CACHE_PATH}/{session_id}"
    if not os.path.exists(file_path):
        return jsonify({'status': 'fail'})
    session_text = to_chat_text(file_path)
    return session_text


@app.route('/api/chat_history/latest_text', methods=['GET'])
def read_latest_text():
    chat_history_list = read_chat_history_list()
    file_path = f"{CACHE_PATH}/{chat_history_list[-1]}"
    session_text = to_chat_text(file_path)
    return session_text


def to_chat_text(file_path):
    with open(file_path, "r") as f:
        chat_history = json.load(f)
        session_text = ""
        for index in range(len(chat_history)):
            if index % 2 == 1:
                continue
            ask_item = chat_history[index]
            ans_item = chat_history[index + 1]
            session_text = session_text + \
                f'<p>{ask_item["type"]} :{ask_item["data"]["content"]}</p>' + "<br\>"
            session_text = session_text + \
                f'<p>{ans_item["type"]} :{ans_item["data"]["content"]}</p>' + "<br\>"
            session_text = session_text + "<br\><p>-------------------</p><br\>"
    return session_text


@app.route('/api/chat_history/<path:session_id>', methods=['GET'])
def chat_history_json(session_id):
    file_path = f"{CACHE_PATH}/{session_id}"
    if not os.path.exists(file_path):
        return jsonify({'status': 'fail'})
    with open(file_path, "r") as f:
        chat_history = json.load(f)
        return jsonify({'status': 'ok', "chat_history": chat_history})


if __name__ == '__main__':
    SERVER_PORT = int(os.environ.get("SERVER_PORT", "5010"))
    logger.info(f"SERVER_PORT:{SERVER_PORT}")
    app.run(host="0.0.0.0", debug=True, port=SERVER_PORT)
