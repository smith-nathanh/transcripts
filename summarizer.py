import os
import re
import json
import logging
import requests
from youtube_transcript_api import YouTubeTranscriptApi
from langchain_openai import ChatOpenAI
from langchain_ollama import ChatOllama
from langchain_cerebras import ChatCerebras
from langchain_groq import ChatGroq
from langchain_text_splitters import RecursiveCharacterTextSplitter

class TranscriptSummarizer:
    def __init__(self, channel, video_id, title='', model="gpt-4o", provider='openai', prompt="prompt.json", temperature=0.3, chunk_size=4000, verbose=False, db_conn=None):
        self.channel = channel
        self.video_id = video_id
        self.title = title
        self.model = model
        self.provider = provider
        self.prompt = prompt
        self.temperature = temperature
        self.chunk_size = chunk_size
        self.verbose = verbose
        self.transcript = ""
        self.summary = ""
        self.output_file = ""
        self.db_conn = db_conn
        self.llm = self._initialize_llm()

    def _get_available_ollama_models(self):
        """Get list of available Ollama models"""
        try:
            response = requests.get("http://localhost:11434/api/tags", timeout=2)
            if response.status_code == 200:
                models = response.json().get('models', [])
                return [model['name'] for model in models]
            return []
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            logging.info("Could not connect to Ollama service")
            return []

    def _initialize_llm(self):
        """Initialize an LLM based on the provider and model"""
        if self.provider == 'ollama':
            ollama_models = self._get_available_ollama_models()
            if self.model in ollama_models:
                try:
                    logging.info(f"Attempting to use Ollama model: {self.model}")
                    return ChatOllama(
                        model=self.model,
                        temperature=self.temperature
                    )
                except Exception as e:
                    logging.error(f"Error initializing Ollama: {e}")
        
        if self.provider == 'cerebras':
            # Check if the model is one of the specified models for ChatCerebras
            cerebras_models = ['llama3.1-8b', 'llama3.1-70b']
            if self.model in cerebras_models:
                if not os.getenv("CEREBRAS_API_KEY"):
                    raise ValueError("CEREBRAS_API_KEY not found in environment variables")
                try:
                    logging.info(f"Using Cerebras model: {self.model}")
                    return ChatCerebras(
                        model=self.model,
                        temperature=self.temperature
                    )
                except Exception as e:
                    logging.error(f"Error initializing Cerebras: {e}")
            else:
                logging.info(f"Cerebras model {self.model} not available")
        
        if self.provider == "groq":
            # Check if the model is one of the specified models for ChatGroq
            groq_models = ['llama-3.3-70b-versatile', 'llama-3.1-8b-instant', 'mixtral-8x7b-32768']
            if self.model in groq_models:
                if not os.getenv("GROQ_API_KEY"):
                    raise ValueError("GROQ_API_KEY not found in environment variables")
                try:
                    logging.info(f"Using Groq model: {self.model}")
                    return ChatGroq(
                        model=self.model,
                        temperature=self.temperature
                    )
                except Exception as e:
                    logging.error(f"Error initializing ChatGroq: {e}")
            else:
                logging.info(f"Groq model {self.model} not available")
        
        if self.provider == "deepseek":
            # Check if the model is one of the specified models for DeepSeek
            deepseek_models = ["deepseek-chat"]
            if self.model in deepseek_models:
                if not os.getenv("DEEPSEEK_API_KEY"):
                    raise ValueError("DEEPSEEK_API_KEY not found in environment variables")
                try:
                    logging.info(f"Using DeepSeek model: {self.model}")
                    return ChatOpenAI(
                        api_key=os.getenv("DEEPSEEK_API_KEY"),
                        base_url="https://api.deepseek.com",
                        temperature=self.temperature,
                        model_name=self.model,
                        verbose=self.verbose
                    )
                except Exception as e:
                    logging.error(f"Error initializing DeepSeek: {e}")
            else:
                logging.info(f"Model {self.model} not available")

        # Use OpenAI as default/fallback
        logging.info("Using ChatOpenAI")
        if not os.getenv("OPENAI_API_KEY"):
            raise ValueError("OPENAI_API_KEY not found in environment variables")
        try:
            return ChatOpenAI(
                temperature=self.temperature,
                model_name=self.model,
                verbose=self.verbose
            )
        except Exception as e:
            logging.error(f"Error initializing ChatOpenAI: {e}")
            raise
    
    def fetch_transcript(self):
        if self.db_conn:
            cur = self.db_conn.cursor()
            cur.execute("""
                SELECT t.transcript 
                FROM transcripts t
                JOIN videos v ON t.video_id = v.id 
                WHERE v.video_id = %s
            """, (self.video_id,))
            result = cur.fetchone()
            cur.close()
            
            if result:
                self.transcript = result[0]
                return

        raw_transcript = YouTubeTranscriptApi.get_transcript(self.video_id)
        self.transcript = ' '.join([line['text'] for line in raw_transcript])
        self.transcript = self._clean_transcript_string(self.transcript)

    def summarize(self):
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.chunk_size,
            chunk_overlap=200,
            length_function=len,
        )
        chunks = text_splitter.split_text(self.transcript)

        with open(self.prompt, 'r') as f:
            prompts = json.load(f)

        map_template = prompts.get("map_prompt", 
            """Write a concise summary of this video transcript section:
            {text}
            CONCISE SUMMARY:""")

        combine_template = prompts.get("combine_prompt", 
            """Below are summaries from different sections of the same video. 
            Create a single coherent summary that captures the key points (avoid saying the word 'delves' during your summarization):

            {text}

            FINAL SUMMARY:""")

        summaries = []
        for chunk in chunks:
            response = self.llm.invoke(map_template.format(text=chunk))
            summary_text = response.content if hasattr(response, 'content') else str(response)
            summaries.append(summary_text)

        # Handle final summary
        final_summary = self.llm.invoke(combine_template.format(
            text="\n".join(summaries)
        ))
        
        self.summary = (final_summary.content 
                       if hasattr(final_summary, 'content') 
                       else str(final_summary))
        
        if self.verbose:
            logging.info(self.summary)

    def save_summary(self):
        self.output_file = self._save_response(self.transcript, self.summary)

    def generate_thread(self):
        paragraphs = self._create_thread_paragraphs(self.summary, self.title, self.channel, self.video_id, self.verbose)
        thread_file = self.output_file.replace('.json', '_thread.txt')
        self._save_thread(thread_file, paragraphs)
        if self.verbose:
            logging.info("\n".join(paragraphs))

    def _clean_transcript_string(self, text):
        text = text.replace('\xa0', ' ')
        text = text.replace('\n', ' ')
        text = text.encode('ascii', 'ignore').decode('ascii')
        text = re.sub(r'\s+', ' ', text)
        text = text.strip()
        return text

    def _save_response(self, transcript, summary):
        channel_dir = self.channel.replace(' ', '_')
        output_dir = os.path.join('texts', channel_dir)
        os.makedirs(output_dir, exist_ok=True)
        output_file = os.path.join(output_dir, f"{self.video_id}.json")
        response = {
            'channel': self.channel,
            'video_id': self.video_id,
            'title': self.title,
            'transcript': transcript,
            'summary': summary
        }
        with open(output_file, 'w') as f:
            json.dump(response, f, indent=4)
        logging.info(f"Summary saved to {output_file}")
        return output_file

    def _create_thread_paragraphs(self, summary, title=None, channel=None, video_id=None, verbose=True):
        paragraphs = []
        
        if title and channel:
            paragraphs.append(self.format_first_paragraph(title, channel))
        
        summary = ' '.join(summary.split())
        sentences = [s.strip() for s in summary.split('. ') if s.strip()]
        
        current_paragraph = []
        current_length = 0
        
        for sentence in sentences:
            new_length = current_length + len(sentence) + (2 if current_paragraph else 0)
            
            if new_length <= 275:
                current_paragraph.append(sentence)
                current_length = new_length
            else:
                if current_paragraph:
                    paragraph_text = '. '.join(current_paragraph) + '.'
                    if verbose:
                        logging.info(f"Paragraph length: {len(paragraph_text)}")
                    paragraphs.append(paragraph_text)
                
                current_paragraph = [sentence]
                current_length = len(sentence)
        
        if current_paragraph:
            paragraph_text = '. '.join(current_paragraph) + '..'
            if verbose:
                logging.info(f"Final paragraph length: {len(paragraph_text)}")
            paragraphs.append(paragraph_text)
        
        paragraphs.append((f"Summary generated by {self.model.split('/')[-1]}.\n"
                           f"🔗 Watch the full video here: https://www.youtube.com/watch?v={video_id}"))
        
        return paragraphs

    def format_first_paragraph(self, title, channel):
        return (f"AI Research Highlights ✨ Distilling AI content into focused summaries "
                f"you can read in minutes. Today's video: {title} by {channel}\n"
                "Full summary in the 🧵 below 👇")

    def _save_thread(self, output_file, paragraphs):
        try:
            with open(output_file, 'w') as f:
                f.write('\n\n'.join(paragraphs))
            logging.info(f"Thread saved to {output_file}")
        except Exception as e:
            logging.error(f"Error saving output: {e}")