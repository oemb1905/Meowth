from meowth import Cog, command, bot, checks
from meowth.exts.map import Gym, ReportChannel, Mapper
from meowth.exts.raid import Raid
from meowth.utils import formatters, snowflake
from meowth.utils.converters import ChannelMessage

import asyncio
from datetime import datetime

class Train:

    instances = dict()
    by_channel = dict()

    def __new__(cls, train_id, *args, **kwargs):
        if train_id in cls.instances:
            return cls.instances[train_id]
        instance = super().__new__(cls)
        cls.instances[train_id] = instance
        return instance

    def __init__(self, train_id, bot, guild_id, channel_id, report_channel_id):
        self.id = train_id
        self.bot = bot
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.report_channel_id = report_channel_id
        self.current_raid = None
        self.report_msgs = []
    
    def to_dict(self):
        d = {
            'id': self.id,
            'guild_id': self.guild_id,
            'channel_id': self.channel_id,
            'report_channel_id': self.report_channel_id,
            'current_raid_id': self.current_raid.id if self.current_raid else None,
            'next_raid_id': self.next_raid.id if self.next_raid else None,
            'report_msgs': self.report_msgs
        }
        return d
    
    @property
    def _data(self):
        table = self.bot.dbi.table('trains')
        query = table.query.where(id=self.id)
        return query
    
    @property
    def _insert(self):
        table = self.bot.dbi.table('trains')
        insert = table.insert
        d = self.to_dict()
        insert.row(**d)

    async def upsert(self):
        insert = self._insert
        await insert.commit(do_update=True)
    
    @property
    def guild(self):
        return self.bot.get_guild(self.guild_id)
    
    @property
    def channel(self):
        return self.bot.get_channel(self.channel_id)
    
    @property
    def report_channel(self):
        rchan = self.bot.get_channel(self.report_channel_id)
        return ReportChannel(self.bot, rchan)
    
    async def possible_raids(self):
        idlist = await self.report_channel.get_all_raids()
        return [Raid.instances.get(x) for x in idlist]
    
    async def select_raid(self, raid):
        raid.channel_ids.append(str(self.channel_id))
        if raid.status == 'active':
            embed = await raid.raid_embed()
        elif raid.status == 'egg':
            embed = await raid.egg_embed()
        raidmsg = await self.channel.send(embed=embed)
        react_list = raid.react_list
        for react in react_list:
            if isinstance(react, int):
                react = self.bot.get_emoji(react)
            await raidmsg.add_reaction(react)
        idstring = f'{self.channel_id}/{raidmsg.id}'
        raid.message_ids.append(idstring)
        await raid.upsert()
        Raid.by_message[idstring] = raid
        Raid.by_channel[str(self.channel_id)] = raid
        self.current_raid = raid
        self.next_raid = None
        await self.upsert()
        await self.poll_next_raid()
    
    async def finish_current_raid(self):
        raid = self.current_raid
        raid.channel_ids.remove(str(self.channel_id))
        for msgid in raid.message_ids:
            if msgid.startswith(str(self.channel_id)):
                try:
                    chn, msg = await ChannelMessage.from_id_string(msgid)
                    await msg.delete()
                except:
                    pass
                raid.message_ids.remove(msgid)
        await raid.upsert()
        if not self.poll_task.done():
            self.poll_task.cancel()
            await self.poll_task
        await self.select_raid(self.next_raid)
        

    async def select_first_raid(self, author):
        raids = await self.possible_raids()
        react_list = formatters.mc_emoji(len(raids))
        choice_embed = await self.display_choices(raids, react_list)
        content = "Select your first raid from the list below!"
        multi = await self.channel.send(content, embed=choice_embed)
        payload = await formatters.ask(self.bot, [multi], user_list=[author.id], 
            react_list=react_list)
        choice_dict = dict(zip(react_list, raids))
        first_raid = choice_dict[str(payload.emoji)]
        await self.select_raid(first_raid)
    
    async def poll_next_raid(self):
        raids = await self.possible_raids()
        if self.current_raid:
            raids.remove(self.current_raid)
        react_list = formatters.mc_emoji(len(raids))
        choice_embed = await self.display_choices(raids, react_list)
        content = "Vote on the next raid from the list below!"
        multi = await self.channel.send(content, embed=choice_embed)
        self.poll_task = self.bot.loop.create_task(formatters.poll(self.bot, [multi],
            react_list=react_list))
        try:
            results = await self.poll_task
        except asyncio.CancelledError:
            results = self.poll_task.result()
        emoji = results[0][0]
        choice_dict = dict(zip(react_list, raids))
        self.next_raid = choice_dict[str(emoji)]
    
    async def display_choices(self, raids, react_list):
        dest_dict = {}
        eggs_list = []
        hatched_list = []
        active_list = []
        if self.current_raid:
            if isinstance(self.current_raid.gym, Gym):
                origin = self.current_raid.gym.id
                known_dest_ids = [x.id for x in raids if isinstance(x.gym, Gym)]
                dests = [Raid.instances[x].gym.id for x in known_dest_ids]
                times = await Mapper.get_travel_times(self.bot, [origin], dests)
                dest_dict = {}
                for d in times:
                    if d['origin_id'] == origin and d['dest_id'] in dests:
                        dest_dict[d['dest_id']] = d['travel_time']
        urls = {x.id: await self.route_url(x) for x in raids}
        react_list = formatters.mc_emoji(len(raids))
        for i in range(len(raids)):
            x = raids[i]
            e = react_list[i]
            summary = f'{e} {await x.summary_str()}'
            if x.gym.id in dest_dict:
                travel = f'Travel Time: {dest_dict[x.gym.id]//60} mins'
            else:
                travel = "Travel Time: Unknown"
            directions = f'[{travel}]({urls[x.id]})'
            summary += f"\n{directions}"
            if x.status == 'egg':
                eggs_list.append(summary)
            elif x.status == 'hatched':
                hatched_list.append(summary)
            elif x.status == 'active':
                active_list.append(summary)
        fields = {}
        if active_list:
            fields['Active'] = "\n\n".join(active_list) + "\n\u200b"
        if hatched_list:
            fields['Hatched'] = "\n\n".join(hatched_list) + "\n\u200b"
        if eggs_list:
            fields['Eggs'] = "\n\n".join(eggs_list)
        embed = formatters.make_embed(title="Raid Choices", fields=fields)
        return embed
    
    async def route_url(self, next_raid):
        if isinstance(next_raid.gym, Gym):
            return await next_raid.gym.url()
        else:
            return next_raid.gym.url
    
    async def new_raid(self, raid: Raid):
        embed = await TrainEmbed.from_raid(self, raid)
        content = "Use the reaction below to vote for this raid next!"
        msg = await self.channel.send(content, embed=embed)
        await msg.add_reaction('\u2b06')


class TrainCog(Cog):

    def __init__(self, bot):
        self.bot = bot
        self.bot.loop.create_task(self.add_listeners())
    
    async def add_listeners(self):
        if self.bot.dbi.train_listener:
            await self.bot.dbi.pool.release(self.bot.dbi.train_listener)
        self.bot.dbi.train_listener = await self.bot.dbi.pool.acquire()
        newraid = ('train', self._newraid)
        await self.bot.dbi.train_listener.add_listener(*newraid)
        
    
    @command()
    async def train(self, ctx):
        name = f'raid-train-{ctx.channel.name}'
        cat = ctx.channel.category
        ow = dict(ctx.channel.overwrites)
        train_channel = await ctx.guild.create_text_channel(name, category=cat, overwrites=ow)
        train_id = next(snowflake.create())
        new_train = Train(train_id, self.bot, ctx.guild.id, train_channel.id, ctx.channel.id)
        await new_train.upsert()
        Train.by_channel[train_channel.id] = new_train
        await new_train.select_first_raid(ctx.author)
    
    @command()
    async def done(self, ctx):
        train = Train.by_channel.get(ctx.channel.id)
        if not train:
            return
        await train.finish_current_raid()

class TrainEmbed():

    def __init__(self, embed):
        self.embed = embed
    
    @classmethod
    async def from_raid(cls, train: Train, raid: Raid):
        if raid.status == 'active':
            bossfield = "Boss"
            boss = raid.pkmn
            name = await boss.name()
            type_emoji = await boss.type_emoji()
            shiny_available = await boss._shiny_available()
            if shiny_available:
                name += " :sparkles:"
            name += f" {type_emoji}"
            img_url = await boss.sprite_url()
        elif raid.status == 'egg':
            bossfield = "Level"
            name = raid.level
            img_url = raid.bot.raid_info.egg_images[level]
        bot = raid.bot
        end = raid.end
        enddt = datetime.fromtimestamp(end)
        # color = await boss.color()
        gym = raid.gym
        travel_time = "Unknown"
        if isinstance(gym, Gym):
            directions_url = await gym.url()
            directions_text = await gym._name()
            exraid = await gym._exraid()
            if train.current_raid:
                current_gym = train.current_raid.gym
                if isinstance(current_gym, Gym):
                    times = await Mapper.get_travel_times(bot, [current_gym], [gym])
                    travel_time = times[0]['travel_time']
        else:
            directions_url = gym.url
            directions_text = gym._name + " (Unknown Gym)"
            exraid = False
        if exraid:
            directions_text += " (EX Raid Gym)"
        fields = {
            bossfield: name,
            "Gym": f"[{directions_text}]({directions_url})",
            "Travel Time": travel_time
        }
        embed = formatters.make_embed(title="Raid Report", # msg_colour=color,
            thumbnail=img_url, fields=fields, footer="Ending")
        embed.timestamp = enddt
        return cls(embed)



